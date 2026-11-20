/* Operational map controls share the living map's company selection and clock. */
const processRoadKey = (source, target) => JSON.stringify([source, target]);

function processFlowControls() {
    return {
        flowMode: 'observed', simulation: null, simulationWeeks: 13, demandChange: 0, departmentOverrides: {},
        get processReport() { return this.map?.process_mining; },
        get isSimulating() { return this.flowMode === 'simulation' && !!this.simulation; },
        get processFrame() { return this.isSimulating ? this.simulation.weeks[this.week - 1] : null; },
        get showsCausalRoads() {
            return this.flowMode === 'impact' || this.map?.flow_roads == null || !!this.alertTrace?.path.length;
        },
        get flowSourceLabel() {
            if (this.showsCausalRoads) return 'افتراضي';
            return { actual: 'من بيانات فعلية', synthetic: 'سجل تجريبي', default: 'افتراضي' }[this.map.flow_source];
        },
        get roads() {
            return this.showsCausalRoads ? this.causalRoads : this.observedRoads;
        },
        resetProcessFlow(map) {
            this.flowMode = 'observed';
            this.simulation = null;
            this.observedRoads = (map.flow_roads || []).map(row => ({
                key: processRoadKey(row.from_department, row.to_department), from: row.from_department,
                to: row.to_department, weight: row.weight,
            }));
            this.simulationWeeks = 13;
            this.demandChange = 0;
            this.departmentOverrides = Object.fromEntries((map.process_mining?.departments || [])
                .map(row => [row.id, { capacity: row.capacity, service: 0 }]));
        },
        processOnlyMap(map) {
            return { ...map, model: { name: map.company.name }, process_only: true, nodes: [], edges: [],
                drivers: [], goal_drivers: [], kpis: [], has_engines: false };
        },
        uploadProcessLog(event) {
            const file = event.target.files[0];
            if (!file) return;
            const data = new FormData();
            data.set('file', file);
            event.target.value = '';
            this.load(this.company.id, this.selectedModel, {
                processImport: { endpoint: 'import', options: { method: 'POST', body: data } },
            });
        },
        seedProcessLog() {
            this.load(this.company.id, this.selectedModel, {
                processImport: { endpoint: 'demo', options: { method: 'POST' } },
            });
        },
        runSimulation() {
            const departments = this.processReport.departments;
            const capacities = departments.map(row => [row.id, Number(this.departmentOverrides[row.id].capacity)]);
            const services = departments.map(row => [row.id, Number(this.departmentOverrides[row.id].service)]);
            const scenario = { demand_pct: Number(this.demandChange), capacities: Object.fromEntries(capacities),
                service_pct: Object.fromEntries(services) };
            return this.work('simulate', async live => {
                const result = await api('/enterprise/simulate', post({
                    company_id: this.company.id, revision: this.processReport.revision,
                    weeks: Number(this.simulationWeeks), seed: 42, scenario,
                }));
                if (!live()) return;
                this.simulation = result;
                this.flowMode = 'simulation';
                this.replay();
            });
        },
        showFlows() {
            this.selectAlert('');
            this.stop();
            this.flowMode = this.simulation ? 'simulation' : 'observed';
            this.week = this.simulation ? 1 : 0;
        },
        showImpact() {
            this.flowMode = 'impact';
            this.replay();
        },
        flowEffects() {
            const effects = new Map();
            const progress = this.clock ?? 1;
            const waiting = new Set(this.processFrame.departments.filter(row => row.waiting > 0).map(row => row.id));
            for (const row of this.processFrame.flows) {
                const key = processRoadKey(row.from_department, row.to_department);
                effects.set(key, { progress: Math.min(progress * 1.25, 1),
                    pulse: Math.max(0, (progress - .8) * 5), adverse: waiting.has(row.to_department) });
            }
            return effects;
        },
    };
}
