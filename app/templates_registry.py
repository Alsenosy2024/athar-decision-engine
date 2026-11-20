from datetime import date
from functools import lru_cache
from math import isfinite
from typing import Literal

from pydantic import BaseModel, Field, JsonValue, computed_field, model_validator

from app.config import DATA_DIR
from app.schemas import TemplateId


class FieldSpec(BaseModel):
    name: str
    type: Literal['str', 'int', 'float', 'bool']
    required: bool
    name_ar: str


class EntitySpec(BaseModel):
    name_ar: str
    key: str | list[str] | None
    fields: list[FieldSpec]

    @property
    def key_fields(self):
        return [self.key] if isinstance(self.key, str) else self.key or []


class Relation(BaseModel):
    source: str = Field(alias='from')
    target: str = Field(alias='to')


class Parameter(BaseModel):
    id: str
    name_ar: str
    type: Literal['int', 'float', 'date', 'dict']
    default: JsonValue
    min: float | None = None

    def accepts(self, value):
        if self.type == 'dict':
            return isinstance(value, dict)
        if self.type == 'date':
            try:
                return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
            except ValueError:
                return False
        valid = type(value) is int if self.type == 'int' else type(value) in (int, float)
        return valid and (type(value) is int or isfinite(value)) and (self.min is None or value >= self.min)


class Constraint(BaseModel):
    id: str
    name_ar: str
    kind: Literal['hard', 'soft', 'hard_soft']
    editable: bool
    param: str | None = None


class Weight(BaseModel):
    id: str
    name_ar: str
    default: float
    min: float
    max: float
    step: float


class TemplateSummary(BaseModel):
    template_id: TemplateId
    name_ar: str
    name_en: str
    time_unit: Literal['week', 'day']
    horizon_key: str


class SectorTemplate(TemplateSummary):
    entities: dict[str, EntitySpec]
    relations: list[Relation]
    parameters: list[Parameter]
    constraints: list[Constraint]
    objective_weights: list[Weight]
    upload_sheets: dict[str, list[str]]
    whatif_changes: list[str]

    @computed_field
    @property
    def scenario_schema(self) -> dict:
        from app.llm.schema import Scenario

        return Scenario.model_json_schema()

    @model_validator(mode='after')
    def validate_structure(self):
        fields = {key: [field.name for field in entity.fields] for key, entity in self.entities.items()}
        if fields != self.upload_sheets:
            raise ValueError('Upload columns must match entity fields')
        for key, entity in self.entities.items():
            if len(fields[key]) != len(set(fields[key])) or not set(entity.key_fields) <= set(fields[key]):
                raise ValueError(f'Invalid fields or key for {key}')
        references = {f'{table}.{column}' for table, columns in fields.items() for column in columns}
        for relation in self.relations:
            if relation.source not in references or relation.target not in references:
                raise ValueError('Invalid relation')
        for entries in (self.parameters, self.constraints, self.objective_weights):
            if len({entry.id for entry in entries}) != len(entries):
                raise ValueError('Duplicate template ID')
        params = {entry.id: entry for entry in self.parameters}
        if self.horizon_key not in params or params[self.horizon_key].type != 'int':
            raise ValueError('Invalid horizon parameter')
        if any(not entry.accepts(entry.default) for entry in self.parameters):
            raise ValueError('Invalid parameter default')
        if any(rule.param is not None and rule.param not in params for rule in self.constraints):
            raise ValueError('Invalid constraint parameter')
        if any(not weight.min <= weight.default <= weight.max or weight.step <= 0 for weight in self.objective_weights):
            raise ValueError('Invalid weight')
        return self

    def model_defaults(self):
        return {
            'parameters': {item.id: item.model_copy(deep=True).default for item in self.parameters},
            'constraints': {item.id: 'soft' if item.kind == 'soft' else 'hard' for item in self.constraints},
            'weights': {item.id: item.default for item in self.objective_weights},
        }


@lru_cache
def load_templates():
    templates = {}
    for path in sorted((DATA_DIR / 'templates').glob('*.json')):
        template = SectorTemplate.model_validate_json(path.read_text(encoding='utf-8'))
        if template.template_id != path.stem:
            raise ValueError(f'Template ID mismatch: {path.name}')
        templates[template.template_id] = template
    return templates
