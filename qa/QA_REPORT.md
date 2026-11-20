# تقرير اختبار المرحلة الثامنة

**20 نوفمبر 2026 · Decision Engine MVP**

## التغطية

اتنفّذ نفس المسار من واجهة المشروع العربية على شركتي الديمو، المقاولات والإنتاج، باستخدام Chromium الفعلي وFastAPI على `127.0.0.1:8780` وقاعدة SQLite مؤقتة لكل تشغيل للاختبار. الأتمتة شغّلت `run.sh` وانتظرت فحص الصحة، ثم قفلت العملية وتأكدت إن المنفذ اتقفل. [خطة Phase 8](<local-path>) · [اختبار Playwright](e2e.py).

| الخطوة | اللي اتحقق من الواجهة |
| --- | --- |
| اختيار الشركة وتحميل الديمو | ظهر اسم مجموعة البيانات النشطة ورسالة نجاح التحميل. |
| نموذج الشركة | اتحفظ الإصدار الثاني بقيم حد الائتمان الافتراضية للديمو، 4,000 ألف ريال للمقاولات و150,000 ريال للإنتاج. |
| التوقعات | اتولّد التوقع وظهر الرسم الفعلي. |
| التحسين | اتطابقت تكلفة الخطتين في الجدول مع نتيجة التشغيل، واتحقق الشرط إن تكلفة الخطة المحسّنة أقل من أو تساوي خط الأساس. |
| ماذا لو | اتنفّذ خفض حد الائتمان 10% للمقاولات وتوقّف خط لمدة يومين للإنتاج. اتأكدت الاختبارات من معاملات الطلب اللي أرسلته الواجهة، وظهر ناتج السيناريو ورسم المحاكاة بعد 200 سحبة. |
| المساعد الذكي | اتملى حقل المحادثة بسؤال خفض الائتمان في القالبين. ظهر رد رقمي واتطابق مع الرد اللي أرجعه `rules`، وظهر رابط السيناريو. |
| الاعتمادات | اتسجلت مسودة من آخر تشغيل تحسين، واتعمدت باسم المراجع وتعليقه. ظهر الاعتماد والتعليق في صف الخطة. |
| تنزيل Excel | Playwright استقبل التنزيل من `export.xlsx`. اتفتح الملف بـ`openpyxl` واتراجعت أسماء الأوراق ووجود بياناتها وتطابق مؤشرات الخطة المعتمدة مع نتيجة التحسين. ظهر قيد التصدير في سجل المراجعة. |
| الفعلي مقابل المخطط | اترفع ملف CSV الخاص بكل قالب، وظهر رسم المقارنة، واتحفظت المعايرة وظهرت رسالة نجاحها. |
| لوحة التحكم | ظهر الجدول الزمني أو مخطط الإنتاج ورسم السيولة للقالبين. شُغّل الوضع الداكن وعاد الوضع الفاتح. |

## إصلاح ظهر في الاختبار

Chromium سجل أخطاء HTTP 404 عند تحميل الحالة الفاضية لآخر تشغيل أو توقع أو مقارنة فعلية. الواجهة دلوقتي بتطلب `allow_missing=true` للمسارات دي، وبتستقبل `HTTP 200` بقيمة `null` للحالة الفاضية. الاختبار بيعدّ كل `console.error` و`pageerror`، واختبار الـAPI بيغطي السلوك الافتراضي للطلب والشركة غير الموجودة. [كود الواجهة](../static/js/app.js) · [اختبار الرجوع](../tests/test_p7_api_smoke.py).

في عرض 390 بكسل، بعض تسميات رسم المقارنة المالية كانت بتتقَص عند حافة الرسم. التسميات في العرض الضيق بتتوزّع دلوقتي على سطور عند حدود الكلمات. Playwright بيختبر التفاف التسميات في القالبين. [كود الرسم](../static/js/charts.js) · [اختبار المتصفح](e2e.py).

## النتائج

**214 اختبار pytest ناجح، ومسارا Playwright ناجحان، و0 أخطاء متصفح و0 طلبات خارجية.** اتقفل المنفذان 8780 و8781 بعد الفحص.

### المقاولات

الحل `OPTIMAL`. وفّر 40.29% من إجمالي تكلفة الديمو. تكلفة سيناريو ماذا لو 1,228.7 ألف ريال.

| المؤشر | خط الأساس (baseline) | المحسّن | الفرق |
| --- | ---: | ---: | ---: |
| إجمالي التكلفة (ألف ريال) | 1,706.3 | 1,018.9 | -687.4 |
| غرامات التأخير (ألف ريال) | 1,325 | 650 | -675 |
| التمويل (ألف ريال) | 381.3 | 368.9 | -12.4 |
| إجمالي أسابيع التأخير | 38 | 11 | -27 |
| ذروة العجز النقدي (ألف ريال) | 3,937 | 3,950.5 | +13.5 |

محاكاة مونت كارلو للخطة المحسّنة قبل تطبيق السيناريو، 200 سحبة ببذرة 42. احتمال وجود تأخير 100% واحتمال تجاوز الائتمان 0.5%.

قيم P10 / P50 / P90 لأسابيع التأخير 18 / 30 / 48.1، وللتكلفة 1,340.04 / 1,985.35 / 2,826.89 ألف ريال، ولذروة العجز 3,948.5 / 3,979.75 / 3,997.05 ألف ريال.

### الإنتاج

الحل `FEASIBLE`. وفّر 9.91% من إجمالي تكلفة الديمو. تكلفة سيناريو ماذا لو 22,685.94 ريال.

| المؤشر | خط الأساس (baseline) | المحسّن | الفرق |
| --- | ---: | ---: | ---: |
| إجمالي التكلفة (ريال) | 22,254.29 | 20,047.85 | -2,206.44 |
| الطلبات المتأخرة | 2 | 2 | 0 |
| مرات تبديل المنتج | 10 | 10 | 0 |
| تكلفة التخزين (ريال) | 1,054 | 895.28 | -158.72 |
| ذروة العجز النقدي (ريال) | 140,000 | 140,000 | 0 |
| ذروة إشغال التخزين (%) | 72 | 68.9 | -3.1 |

محاكاة مونت كارلو للخطة المحسّنة قبل تطبيق السيناريو، 200 سحبة ببذرة 42. احتمال وجود تأخير 100% واحتمال تجاوز الائتمان 95%.

احتمال تجاوز التخزين 98%. قيم P10 / P50 / P90 لنسبة تلبية الطلب 51.41% / 66.01% / 81.12%، ولعدد الطلبات المتأخرة أو غير المشحونة 4 / 7 / 9.

## حدود النسخة والاختبار

- التشغيل محلي على loopback. مفيش مصادقة أو صلاحيات أدوار. اسم المراجع حقل إدخال. التشغيل بعملية Uvicorn واحدة وقاعدة SQLite بنمط WAL. [README](../README.md) · [تهيئة القاعدة](../app/db.py).
- `rules` هو المزوّد الافتراضي، وهو الوحيد اللي اتختبر في المتصفح. الردود مبنية على العبارات المدعومة والبيانات المحسوبة. ما اتعملش أي استدعاء لـGemini أو لأي مزوّد LLM خارجي. [إعدادات المشروع](../app/config.py).
- بيانات الاختبار تجريبية. وفورات الإنتاج في التشغيل ده حوالي 10% على إجمالي التكلفة، وتفاصيل مؤشرات التأخير والتخزين لازم تتقري مع التكلفة. حل الإنتاج المحدود بالوقت ممكن يرجّع حلول قابلة للتنفيذ بمؤشرات مختلفة بين التشغيلات. التخطيط للإنتاج بدُفعات زمنية يومية. [قالب الإنتاج](../app/data/templates/production.json) · [حدود تشغيل المحلّل](../app/optimization/production.py).
- تصدير P6 XML للمقاولات بيغطي مجموعة محدودة من الحقول. التحقق في المرحلة دي شمل تنزيل Excel وفتح الملف، واستيراد P6 داخل Primavera خارج التغطية. [كود التصدير](../app/delivery/export.py) · [تقرير المرحلة السادسة](phase6.md).
- وزن Regular لخط Thmanyah Serif Text غير متاح ضمن الأصول. CSS بيستخدم ملف Bold لأوزان 400–700. فحص التثبيت النظيف اتنفّذ على نفس الماك، وفيه خطوط Thmanyah المطلوبة ومتصفح Chromium مخزّن بالفعل. `run.sh` بيعتمد حاليًا على Python في `/usr/local/bin/python3` وعلى الخطوط في `~/Library/Fonts`. [CSS](../static/css/app.css) · [بناء الأصول](../scripts/build_assets.py) · [التشغيل](../run.sh).
- مراجعة الواجهة على Chromium المكتبي ومحاكاة عرض 390 بكسل. جداول المقارنة العريضة لها تمرير أفقي داخلي على الموبايل، وبعض تسميات محاور الرسم بتختصر تلقائيًا مع العرض الضيق. Safari وFirefox والأجهزة الفعلية خارج التغطية.

## اللقطات النهائية

اتراجعت 26 لقطة بصريًا. ترتيب الواجهة من اليمين للشمال والحروف العربية متصلة. اتقاس عرض ملفات PNG، واتفحصت بكسلات كل Canvas ظاهر مع بيانات رسم محمّلة. كل رسم عدى حد 1% بكسلات مرسومة. اللقطات كاملة الصفحة من نافذتي 1440×900 و390×844.

| القالب | اللقطة وأبعادها بالبكسل | الفحص البصري |
| --- | --- | --- |
| المقاولات | [01_dashboard_1440 · 1440×1586](screens/final_contracting_01_dashboard_1440.png) | جدول الأنشطة ورسم السيولة ظاهرين. |
| المقاولات | [01_dashboard_390 · 390×1949](screens/final_contracting_01_dashboard_390.png) | بطاقات المؤشرات ظاهرة في صفّين والرسومان ظاهرين. |
| المقاولات | [01_dashboard_dark · 1440×1586](screens/final_contracting_01_dashboard_dark.png) | المؤشرات وجدول الأنشطة والسيولة مقروءين على الخلفية الداكنة. |
| المقاولات | [02_data_1440 · 1440×903](screens/final_contracting_02_data_1440.png) | إشعار التحميل وملخص مشاريع الديمو ظاهرين. |
| المقاولات | [03_model_1440 · 1440×924](screens/final_contracting_03_model_1440.png) | الإصدار الثاني وحقول نموذج المقاولات ظاهرين. |
| المقاولات | [04_forecast_1440 · 1440×1900](screens/final_contracting_04_forecast_1440.png) | أعمدة P10/P50/P90 وجدول الاختبار التاريخي ظاهرين. |
| المقاولات | [05_optimize_1440 · 1440×1530](screens/final_contracting_05_optimize_1440.png) | مقارنة التكلفة وجدول المؤشرات ظاهرين. |
| المقاولات | [05_optimize_390 · 390×2332](screens/final_contracting_05_optimize_390.png) | الرسم والنتيجة مقروءين، والجدول قابل للتمرير داخليًا. |
| المقاولات | [06_whatif_1440 · 1440×1625](screens/final_contracting_06_whatif_1440.png) | أثر الائتمان ونتيجة المخاطر والرسم التكراري ظاهرين. |
| المقاولات | [07_chat_1440 · 1440×963](screens/final_contracting_07_chat_1440.png) | السؤال والرد الرقمي ورابط السيناريو ظاهرين. |
| المقاولات | [07_chat_390 · 390×1495](screens/final_contracting_07_chat_390.png) | فقاعات المحادثة والرد ورابط السيناريو داخل عرض الشاشة. |
| المقاولات | [08_approvals_1440 · 1440×1031](screens/final_contracting_08_approvals_1440.png) | حالة الاعتماد والتعليق وقيد Excel ظاهرين. |
| المقاولات | [09_actuals_1440 · 1440×2036](screens/final_contracting_09_actuals_1440.png) | مقارنة فعلية للأنشطة ومعايرة مدد ظاهرة. |
| الإنتاج | [01_dashboard_1440 · 1440×1378](screens/final_production_01_dashboard_1440.png) | جدول الخطوط ورسم السيولة ظاهرين. |
| الإنتاج | [01_dashboard_390 · 390×1774](screens/final_production_01_dashboard_390.png) | بطاقات المؤشرات ظاهرة في صفّين والرسومان ظاهرين. |
| الإنتاج | [01_dashboard_dark · 1440×1378](screens/final_production_01_dashboard_dark.png) | جدول الخطوط والسيولة مقروءين على الخلفية الداكنة. |
| الإنتاج | [02_data_1440 · 1440×903](screens/final_production_02_data_1440.png) | إشعار التحميل وملخص بيانات المصنع ظاهرين. |
| الإنتاج | [03_model_1440 · 1440×1078](screens/final_production_03_model_1440.png) | الإصدار الثاني وحقول نموذج الإنتاج ظاهرين. |
| الإنتاج | [04_forecast_1440 · 1440×1172](screens/final_production_04_forecast_1440.png) | منحنى الطلب ونطاق عدم اليقين ظاهرين. |
| الإنتاج | [05_optimize_1440 · 1440×1752](screens/final_production_05_optimize_1440.png) | مقارنة التكلفة وجدول المؤشرات ظاهرين. |
| الإنتاج | [05_optimize_390 · 390×2773](screens/final_production_05_optimize_390.png) | الرسم والنتيجة مقروءين، والجدول قابل للتمرير داخليًا. |
| الإنتاج | [06_whatif_1440 · 1440×2068](screens/final_production_06_whatif_1440.png) | أثر توقف الخط ونتيجة المخاطر والرسم التكراري ظاهرين. |
| الإنتاج | [07_chat_1440 · 1440×989](screens/final_production_07_chat_1440.png) | السؤال والرد الرقمي ورابط السيناريو ظاهرين. |
| الإنتاج | [07_chat_390 · 390×1586](screens/final_production_07_chat_390.png) | فقاعات المحادثة والرد ورابط السيناريو داخل عرض الشاشة. |
| الإنتاج | [08_approvals_1440 · 1440×1031](screens/final_production_08_approvals_1440.png) | حالة الاعتماد والتعليق وقيد Excel ظاهرين. |
| الإنتاج | [09_actuals_1440 · 1440×1894](screens/final_production_09_actuals_1440.png) | مقارنة الطلبات ومعايرة الإنتاج ظاهرة. |

## مخرجات التشغيل

فحص التثبيت النظيف استخدم نسخة مصدر مؤقتة لأن مجلد المشروع مفيهوش بيانات Git. بدأ من غير بيئة افتراضية أو ملفات قاعدة بيانات، وثبّت الاعتماديات في `.venv` الخاصة بالنسخة، ورجع HTTP 200، وبعدها اتقفلت العملية واتمسحت النسخة بالكامل.

### pytest

```text
$ LLM_PROVIDER=rules .venv/bin/python -m pytest -q -o addopts=
........................................................................ [ 33%]
........................................................................ [ 67%]
......................................................................   [100%]
=============================== warnings summary ===============================
.venv/lib/python3.13/site-packages/starlette/testclient.py:45
  <local-path> DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = Callable[[], AbstractContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
214 passed, 1 warning in 301.24s (0:05:01)
exit=0
```

### Playwright

```text
$ .venv/bin/python -u qa/e2e.py
Health HTTP 200: {"status": "ok", "version": "0.1.0", "solver": "ortools 9.15.6755", "provider": "rules"}
Chromium 145.0.7632.6
final_contracting_02_data_1440.png
final_contracting_03_model_1440.png
final_contracting_04_forecast_1440.png — canvas pixels {'chart-forecast': '21.5%'}
final_contracting_05_optimize_1440.png — canvas pixels {'chart-compare': '19.8%'}
final_contracting_05_optimize_390.png — canvas pixels {'chart-compare': '22.2%'}
final_contracting_06_whatif_1440.png — canvas pixels {'chart-mc': '15.7%'}
final_contracting_07_chat_1440.png
final_contracting_07_chat_390.png
final_contracting_08_approvals_1440.png
final_contracting_09_actuals_1440.png — canvas pixels {'chart-pva': '21.6%'}
final_contracting_01_dashboard_1440.png — canvas pixels {'chart-gantt': '8.2%', 'chart-cash': '23.2%'}
final_contracting_01_dashboard_390.png — canvas pixels {'chart-gantt': '9.2%', 'chart-cash': '23.8%'}
final_contracting_01_dashboard_dark.png — canvas pixels {'chart-gantt': '8.2%', 'chart-cash': '23.2%'}
contracting KPIs: {"solver": "OPTIMAL", "baseline": {"total": 1706.3, "ld": 1325, "interest": 381.3, "late_weeks_total": 38, "peak_od": 3937.0}, "optimized": {"total": 1018.9, "ld": 650, "interest": 368.9, "late_weeks_total": 11, "peak_od": 3950.5}, "saving_pct": 40.29, "scenario_cost": 1228.7}
contracting Monte Carlo: {"n": 200, "p_any_late": 1.0, "p_credit_breach": 0.005, "late_weeks": {"p10": 18.0, "p50": 30.0, "p90": 48.099999999999994, "mean": 31.96}, "total_cost": {"p10": 1340.04, "p50": 1985.35, "p90": 2826.8900000000003}, "peak_od": {"p10": 3948.5, "p50": 3979.75, "p90": 3997.05}}
E2E OK contracting
final_production_02_data_1440.png
final_production_03_model_1440.png
final_production_04_forecast_1440.png — canvas pixels {'chart-forecast': '6.9%'}
final_production_05_optimize_1440.png — canvas pixels {'chart-compare': '14.9%'}
final_production_05_optimize_390.png — canvas pixels {'chart-compare': '15.3%'}
final_production_06_whatif_1440.png — canvas pixels {'chart-mc': '23.9%'}
final_production_07_chat_1440.png
final_production_07_chat_390.png
final_production_08_approvals_1440.png
final_production_09_actuals_1440.png — canvas pixels {'chart-pva': '26.2%'}
final_production_01_dashboard_1440.png — canvas pixels {'chart-plan': '19.2%', 'chart-cash': '35.9%'}
final_production_01_dashboard_390.png — canvas pixels {'chart-plan': '18.7%', 'chart-cash': '32.6%'}
final_production_01_dashboard_dark.png — canvas pixels {'chart-plan': '19.2%', 'chart-cash': '35.9%'}
production KPIs: {"solver": "FEASIBLE", "baseline": {"total_cost": 22254.29, "late_orders": 2, "changeovers": 10, "holding_cost": 1054.0, "peak_od": 140000, "peak_storage_pct": 72.0}, "optimized": {"total_cost": 20047.85, "late_orders": 2, "changeovers": 10, "holding_cost": 895.28, "peak_od": 140000, "peak_storage_pct": 68.9}, "saving_pct": 9.91, "scenario_cost": 22685.94}
production Monte Carlo: {"n": 200, "p_any_late": 1.0, "p_credit_breach": 0.95, "p_storage_overflow": 0.98, "fill_rate": {"p10": 0.5140781472968382, "p50": 0.6601237058449974, "p90": 0.8111710573084292}, "late_orders": {"p10": 4.0, "p50": 7.0, "p90": 9.0}}
E2E OK production
console_errors=0 external_requests=0
Port 8780 closed
exit=0
```

### التثبيت النظيف

```text
Fresh source copy with no .venv, SQLite database, caches or QA screenshots.
$ PORT=8781 ./run.sh
GET /api/health → HTTP 200 {"status": "ok", "version": "0.1.0", "solver": "ortools 9.15.6755", "provider": "rules"}
INFO:     Started server process [74124]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8781 (Press CTRL+C to quit)
INFO:     127.0.0.1:55593 - "GET /api/health HTTP/1.1" 200 OK
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [74124]
Temporary source copy removed. Port 8781 closed.
```
