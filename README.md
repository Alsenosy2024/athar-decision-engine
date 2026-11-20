# أثر | Athar — Decision Intelligence

**أثر** منصة ذكاء قرارات وخريطة حية للشركة: كل قرار.. وأثره على شركتك كلها. تطبيق محلي بواجهة عربية RTL (FastAPI + SQLite + Alpine.js)، بيحوّل بيانات الشركة لنموذج علاقات، ويحسب أثر أي فرضية على التشغيل والأرباح والسيولة والمخاطر.

*Athar is a local, Arabic-first (RTL) decision-intelligence app: it models a company as a causal map, then computes the impact of any "what if" on operations, profit, cash and risk. Demo data is synthetic. Runs fully offline with a rule-based assistant; an LLM is optional.*

## المميزات
- **الخريطة الحية** — خريطة مؤسسية بعلاقات داخلة/خارجة لكل عنصر، وتحديث لحظي للأثر.
- **اسأل أثر** — مساعد بالعربي يحوّل السؤال لفرضيات، ومحرك الأثر يحسب (مش النموذج اللغوي).
- **توقعات P10 / P50 / P90** — Holt/Croston + TimesFM اختياري، مع backtest ومحاكاة مونت كارلو.
- **تعدين العمليات (Process Mining)** — اكتشاف المسارات والاختناقات (pm4py).
- **الموجز (Briefing)** — موجز تلقائي بالشذوذات والمخاطر عند فتح الخريطة.
- **أصحاب البيانات بالأقسام** — إسناد أقسام التجهيز لمخططين بصلاحيات منفصلة.
- **API / Embed / Webhooks** — واجهة عامة `/api/v1`، خريطة مضمّنة بتوكن، وتنبيهات Webhooks.
- أيضًا: تحسين بـ OR-Tools (مقاولات/إنتاج/تجارة)، ماذا لو، مقارنة سيناريوهات وTornado، معايرة وbackfill، ربط Excel/CSV/ERP/Primavera P6، أدوار (admin/planner/approver/viewer).

## المتطلبات
Python 3.11+ (اتجرّب على 3.13)، `curl` (لتحميل Alpine/Chart.js أول مرة لو مش موجودين)، ~2 جيجا للحزم. الخطوط (Thmanyah Serif) متضمّنة في `static/fonts`.

## التشغيل
```bash
git clone https://github.com/<user>/athar-decision-engine.git && cd athar-decision-engine
cp .env.example .env        # عدّل ATHAR_ADMIN_PASSWORD (12–256 حرف)
./run.sh                    # ينشئ .venv ويثبّت requirements.txt ويشغّل على http://127.0.0.1:8780
```
يدويًا:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/build_assets.py
ATHAR_ADMIN_USERNAME=admin ATHAR_ADMIN_PASSWORD='change-me-please-12' .venv/bin/uvicorn app.main:app --port 8780
.venv/bin/pytest -q         # الاختبارات
```
- **لا توجد حسابات افتراضية.** الأدمن بيتنشأ من `ATHAR_ADMIN_USERNAME/PASSWORD` عند أول تشغيل على قاعدة فاضية فقط (Argon2id). بعدها احذف كلمة السر من البيئة.
- **بيانات الديمو**: شركتين تجريبيتين (مقاولات وإنتاج) + نموذج «روافد» المؤسسي — اضغط «حفظ الديمو» في **النموذج المؤسسي**. كل البيانات صناعية.
- **TimesFM (اختياري)**: `bash scripts/setup_timesfm.sh` يجهّز `.venv-tsfm` (~925MB). بدونه التوقع إحصائي تلقائيًا (`FORECAST_ENGINE=auto`).
- OCR اختياري: `scripts/setup_ocr.sh`.

## إعداد الـ LLM
متغير `LLM_PROVIDER` (في `.env`):
- `rules` — محلي بالكامل بدون مفاتيح (موصى به للتجربة).
- `codex` — الافتراضي في الكود؛ بيشغّل `codex exec` بتسجيل دخول `CODEX_HOME` (الافتراضي `~/.codex`)، ولو مش متاح بيرجع للقواعد المحلية تلقائيًا.
- `openai_compat` — مع `OPENROUTER_API_KEY` و`LLM_BASE_URL` و`LLM_MODEL` (مساعد الشركات العادي فقط؛ المساعد المؤسسي يدعم codex/rules).
**ما تحطش مفاتيح في الكود أو تعمل commit لـ `.env`.** الحسابات بتتم بنفس الأرقام من محرك الأثر مهما كان المزود؛ الـ LLM للصياغة والتفسير بس.

باقي المتغيرات: `DB_PATH`, `PORT`, `ATHAR_SECURE_COOKIE`, `ATHAR_EMBED_SECRET` (≥32 بايت), `ATHAR_WEBHOOKS` (JSON), `ATHAR_FRAME_ANCESTORS`, `ATHAR_ERP_REST_URL/TOKEN`.

الدليل التفصيلي بالعربي: [docs/GUIDE_AR.md](docs/GUIDE_AR.md) · تقارير الاختبار: [qa/](qa/)
