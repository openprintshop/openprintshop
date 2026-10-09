"""Инвентарь маршрутов веб-слоя: снимок таблицы роутов и снимок карты прав админки.

Зачем это нужно. app/webapp.py — двенадцать тысяч строк и почти две с половиной сотни ручек, и
разбирать его на модули придётся не одной волной. При таком переносе маршрут пропадает МОЛЧА:
aiohttp не жалуется на незарегистрированную ручку, юнит-тест хендлера продолжает звать функцию
напрямую и остаётся зелёным, а пропажу первым замечает клиент, у которого перестала работать
кнопка. Здесь лежит эталон — полный список троек (метод, канонический путь, имя обработчика)
и полная карта прав для админских путей, снятые с ТЕКУЩЕГО состояния кода.

Тихая пропажа ручки — не единственная беда переноса, и даже не худшая. Худшая — ПОДМЕНА:
путь остался, а отвечает на него теперь чужая функция. Пары (метод, путь) такого не видят
вовсе; проверено свапом двух калибровочных обработчиков — все 1329 тестов остались зелёными.
Потому третья колонка и появилась, и потому же разбор расхождений умеет отдельную категорию
«ПУТЬ ОБСЛУЖИВАЕТ ДРУГАЯ РУЧКА».

Это снимок, а не проект. Тест ничего не оценивает и не требует «как правильно»: он лишь ловит
изменение, которого никто не планировал.

Что делать, когда покраснел:
  • «ПРОПАЛИ маршруты» — ручку потеряли при переносе, чинить надо код, а не эталон;
  • «ПУТЬ ОБСЛУЖИВАЕТ ДРУГАЯ РУЧКА» — обработчики разъехались по путям. Это НЕ повод
    переписать третью колонку: сначала убедиться, что путь и правда должен обслуживаться
    новой функцией, — переименование ручки такое даёт законно, а промах при переносе блока
    выглядит точно так же и уводит клиента в чужую логику с чужими правами;
  • «ПОЯВИЛИСЬ маршруты» — маршрут добавлен законно; тест печатает готовые строки эталона,
    их достаточно вставить в ROUTES/ADMIN_CAPS (по одной строке на маршрут);
  • «ПРАВО ИЗМЕНИЛОСЬ» — сузили или расширили доступ команде (A4). Если так и задумано —
    правим эталон, если нет — это дыра в правах или потеря доступа у оператора.

Правило цикла 2 про этот файл. Он относится к разделу «Страховка», а такие файлы переводятся
на общие фикстуры (post_req/get_req/admin_req) ОТДЕЛЬНЫМ коммитом ПЕРЕД пачкой переносов,
а не попутно с ними. Иначе в одном диффе окажутся и смена эталона, и смена способа его снятия,
и отличить «прибор показал правду» от «прибор перенастроили» будет уже нечем.
"""
import pytest

from app import roles, webapp

# ─────────────────────────── эталон: таблица маршрутов ───────────────────────────
# Снято с ветки test 08.08.2026, третья колонка добавлена 09.08.2026. HEAD здесь СОЗНАТЕЛЬНО
# не перечислен: aiohttp заводит его сам к каждому add_get (allow_head), поэтому в эталоне он был
# бы механическим дублем GET и удваивал бы правку при добавлении одной ручки. Что HEAD и правда
# зеркалит GET — проверяет отдельный тест ниже, так что мимо снимка не проходит ничего.
#
# Третья колонка — `handler.__qualname__`, и она здесь главная. Пары (метод, путь) ловят только
# ПРОПАЖУ ручки: если при переносе два обработчика поменялись местами, все пути остались на месте
# и снимок из пар молчит. Это не теория — свап двух калибровочных ручек прошёл все 1329 тестов
# зелёными. А подмена обработчика — ровно то, что делает перенос кода, ради которого этот файл
# и заведён.
#
# До 22.08.2026 у четырёх строк (_ui_file на трёх путях и _favicon) в колонке стояло
# «make_app.<locals>.» — ручки были замыканиями внутри make_app. Подняты на уровень модуля
# (app/web/uifiles.py, вопрос 5 endgame) — эталон покраснел категорией «ДРУГАЯ РУЧКА» законно,
# и третью колонку поправили руками, как здесь и предписано.
#
# Модуль обработчика в снимок НЕ кладём НАМЕРЕННО. Плановые переезды ручек в app/web/* меняют
# модуль у сотен строк разом, эталон краснел бы каждым разрезом целиком — и его начали бы
# перештамповывать не глядя, то есть прибор перестал бы быть прибором. Имя же ручки при честном
# переносе не меняется вовсе.
#
# ТРЕБОВАНИЕ НА БУДУЩЕЕ, действует с этого коммита: любой декоратор админских ручек обязан быть
# написан через functools.wraps. Без него __qualname__ покажет имя обёртки, и эталон покраснеет
# на всех 105 админских POST разом — а из такой красноты самый лёгкий выход выглядит как «выкинуть
# третью колонку», а не как «дописать одну строку в декоратор».
ROUTES: frozenset[tuple[str, str, str]] = frozenset({
    ("GET", "/", "_collection_page"),
    ("GET", "/admin/photo/{kind}/{id}", "_admin_photo_file"),
    ("GET", "/api/admin/activity", "_api_admin_activity"),
    ("GET", "/api/admin/backup_status", "_api_admin_backup_status"),
    ("GET", "/api/admin/batch_last", "_api_admin_batch_last"),
    ("GET", "/api/admin/batch_status", "_api_admin_batch_status"),
    ("GET", "/api/admin/bigdata", "_api_admin_bigdata"),
    ("GET", "/api/admin/calc_queue", "_api_admin_calc_queue"),
    ("GET", "/api/admin/calib", "_api_admin_calib"),
    ("GET", "/api/admin/calib/drift", "_api_admin_calib_drift"),
    ("GET", "/api/admin/calib/export", "_api_admin_calib_export"),
    ("GET", "/api/admin/calib/labels", "_api_admin_calib_labels"),
    ("GET", "/api/admin/calib/preset_history", "_api_admin_calib_preset_history"),
    ("GET", "/api/admin/calib/presets", "_api_admin_calib_presets"),
    ("GET", "/api/admin/calib/run", "_api_admin_calib_run"),
    ("GET", "/api/admin/camera", "_api_admin_camera"),
    ("GET", "/api/admin/camera_check", "_api_admin_camera_check"),
    ("GET", "/api/admin/chat_search", "_api_admin_chat_search"),
    ("GET", "/api/admin/calc", "_api_admin_calc"),
    ("GET", "/api/admin/client", "_api_admin_client"),
    ("GET", "/api/admin/client_chat", "_api_admin_client_chat"),
    ("GET", "/api/admin/clients", "_api_admin_clients"),
    ("GET", "/api/admin/clients/export.csv", "_api_admin_clients_csv"),
    ("GET", "/api/admin/collection", "_api_admin_collection"),
    ("GET", "/api/admin/dashboard", "_api_admin_dashboard"),
    ("GET", "/api/admin/delivery", "_api_admin_delivery"),
    ("GET", "/api/admin/discounts", "_api_admin_discounts"),
    ("GET", "/api/admin/errors", "_api_admin_errors"),
    ("GET", "/api/admin/events", "_api_admin_events"),
    ("GET", "/api/admin/families", "_api_admin_families"),
    ("GET", "/api/admin/hub", "_api_admin_hub"),
    ("GET", "/api/admin/journal", "_api_admin_journal"),
    ("GET", "/api/admin/kb", "_api_admin_kb"),
    ("GET", "/api/admin/mw_accounts", "_api_admin_mw_accounts"),
    ("GET", "/api/admin/notifications", "_api_admin_notifications"),
    ("GET", "/api/admin/options", "_api_admin_options"),
    ("GET", "/api/admin/order/{id}", "_api_admin_order"),
    ("GET", "/api/admin/orderfile", "_api_admin_orderfile"),
    ("GET", "/api/admin/orders", "_api_admin_orders"),
    ("GET", "/api/admin/orders/export.csv", "_api_admin_orders_csv"),
    ("GET", "/api/admin/plastics", "_api_admin_plastics"),
    ("GET", "/api/admin/presets", "_api_admin_presets"),
    ("GET", "/api/admin/print_classify", "_api_admin_print_classify"),
    ("GET", "/api/admin/printer_files", "_api_admin_printer_files"),
    ("GET", "/api/admin/printer_history", "_api_admin_printer_history"),
    ("GET", "/api/admin/printer_status", "_api_admin_printer_status"),
    ("GET", "/api/admin/printers", "_api_admin_printers"),
    ("GET", "/api/admin/products", "_api_admin_products"),
    ("GET", "/api/admin/promos", "_api_admin_promos"),
    ("GET", "/api/admin/queue", "_api_admin_queue"),
    ("GET", "/api/admin/reprint_parts", "_api_admin_reprint_parts"),
    # «Инструменты» → допечатка со слоя (Путь В, app/web/tools.py)
    ("GET", "/api/admin/tools/resume/sources", "_api_tools_resume_sources"),
    ("GET", "/api/admin/tools/resume/download", "_api_tools_resume_download"),
    ("GET", "/api/admin/tools/resume/job", "_api_tools_resume_job"),
    ("GET", "/api/admin/tools/resume/paths", "_api_tools_resume_paths"),
    ("GET", "/api/admin/tools/resume/gates", "_api_tools_resume_gates"),
    ("GET", "/api/admin/tools/resume/history", "_api_tools_resume_history"),
    # Пульт, вторая очередь (app/web/printerext.py): фичи прошивки сообщества CC2, сделанные своими
    ("GET", "/api/admin/heat_presets", "_api_heat_presets_get"),
    ("GET", "/api/admin/printer_community", "_api_community_get"),
    ("GET", "/api/admin/printer_file_info", "_api_printer_file_info"),
    ("GET", "/api/admin/printer_thumb", "_api_printer_thumb"),
    ("GET", "/api/admin/self_orders", "_api_admin_self_orders"),
    ("GET", "/api/admin/service", "_api_admin_service"),
    ("GET", "/api/admin/settings", "_api_admin_settings"),
    ("GET", "/api/admin/shades", "_api_admin_shades"),
    ("GET", "/api/admin/spools", "_api_admin_spools"),
    ("GET", "/api/admin/staff", "_api_admin_staff"),
    ("GET", "/api/admin/status", "_api_admin_status"),
    ("GET", "/api/admin/tasks", "_api_admin_tasks"),
    ("GET", "/api/admin/usage", "_api_admin_usage"),
    ("GET", "/api/collection", "_api_collection"),
    ("GET", "/api/collection_search", "_api_collection_search"),
    ("GET", "/api/cpreview/{token}", "_api_cpreview"),
    ("GET", "/api/delivery_types", "_api_delivery_types"),
    # Вход в настольную админку (app/desklogin.py): три ручки БЕЗ авторизации — это дверь.
    # Каждая отвечает только на запрос с этой же машины, снаружи их нет (HTTPNotFound).
    ("GET", "/api/desk/whoami", "_api_desk_whoami"),
    ("GET", "/api/feed", "_api_feed"),
    # Gridfinity-конфигуратор (design_handoff_gridfinity, 28.08.2026) — старый контур:
    # черновик и заявка ЦЕЛИКОМ. Живут ради мини-аппа, который ещё не обновился.
    ("GET", "/api/gridfinity/config", "_api_gridfinity_config"),
    ("POST", "/api/gridfinity/config", "_api_gridfinity_config_set"),
    ("POST", "/api/gridfinity/order", "_api_gridfinity_order"),
    ("GET", "/api/admin/gridfinity/{rid}", "_api_admin_gridfinity_config"),
    # «Мастерская органайзеров» (30.08.2026): проекты, спецификация деталей, частичный
    # заказ партиями, мерки, заготовки, шаблон дна и ссылка «показать».
    ("GET", "/api/gridfinity/projects", "_api_gf_projects"),
    ("POST", "/api/gridfinity/projects", "_api_gf_project_add"),
    ("GET", "/api/gridfinity/project/{pid}", "_api_gf_project"),
    ("POST", "/api/gridfinity/project/{pid}", "_api_gf_project_save"),
    ("POST", "/api/gridfinity/project/{pid}/patch", "_api_gf_project_patch"),
    ("POST", "/api/gridfinity/project/{pid}/select", "_api_gf_parts_select"),
    ("GET", "/api/gridfinity/project/{pid}/preflight", "_api_gf_preflight"),
    ("POST", "/api/gridfinity/project/{pid}/order", "_api_gf_order"),
    ("GET", "/api/gridfinity/project/{pid}/template", "_api_gf_template"),
    ("POST", "/api/gridfinity/part/{id}", "_api_gf_part_patch"),
    ("POST", "/api/gridfinity/measures", "_api_gf_measure_add"),
    ("GET", "/api/gridfinity/presets", "_api_gf_presets"),
    ("POST", "/api/gridfinity/presets", "_api_gf_preset_add"),
    ("GET", "/gfx/{token}", "_gf_share_page"),
    ("GET", "/gfx/desk/{token}", "_gf_desk_page"),
    ("POST", "/api/admin/gridfinity/part/{id}", "_api_admin_gf_part_printed"),
    ("POST", "/api/admin/gridfinity/release/{rid}", "_api_admin_gf_release"),
    ("GET", "/api/lcd/data", "_api_lcd_data"),
    ("GET", "/api/model_search", "_api_model_search"),
    ("GET", "/api/modelfile/{token}", "_api_modelfile"),
    ("GET", "/api/mpreview/{token}", "_api_mpreview"),
    ("GET", "/api/mymodels", "_api_mymodels"),
    ("GET", "/api/options", "_api_options"),
    ("GET", "/api/order/messages", "_api_order_messages"),
    ("GET", "/api/orders", "_api_orders"),
    ("GET", "/api/parametric/models", "_api_parametric_models"),
    ("GET", "/api/plate/{token}/{idx}", "_api_plate"),
    ("GET", "/api/plates/{token}", "_api_plates_info"),
    ("GET", "/api/products", "_api_products"),
    ("GET", "/api/profile", "_api_profile"),
    ("GET", "/api/quote_state", "_api_quote_state"),
    ("GET", "/api/recommend", "_api_recommend"),
    ("GET", "/api/screen-power", "_api_screen_power"),
    ("GET", "/api/screen-status", "_api_screen_status"),
    ("GET", "/api/thumb", "_api_thumb"),
    ("GET", "/api/version", "_api_version"),
    ("GET", "/app", "_ui_file"),
    ("GET", "/app/", "_ui_file"),
    ("GET", "/app/{path}", "_ui_file"),
    ("GET", "/avatar/{uid}", "_avatar_file"),
    ("GET", "/batchprev/{bid}/{idx}", "_batch_preview"),
    ("GET", "/brand/logo", "_brand_logo_file"),
    ("GET", "/catalog", "_catalog_page"),
    ("GET", "/csample/{sid}", "_csample_page"),
    ("GET", "/custphoto/{rid}/{idx}", "_custom_photo"),
    ("GET", "/favicon.ico", "_favicon"),
    ("GET", "/health", "_health"),
    ("GET", "/img/{cid}", "_image"),
    ("GET", "/jtl/{jid}", "_jtl_file"),
    ("GET", "/lcd", "_lcd_file"),
    ("GET", "/lcd/", "_lcd_file"),
    ("GET", "/lcd/{path}", "_lcd_file"),
    ("GET", "/local/avatar_gpu/refresh", "_local_avatar_gpu_refresh"),
    ("GET", "/pimg/{pid}", "_plastic_image"),
    ("GET", "/prodphoto/{id}", "_product_photo_file"),
    ("GET", "/reqprev/{rid}", "_request_preview"),
    ("GET", "/slicershot/{rid}", "_slicer_shot_file"),
    ("GET", "/smprev/{sid}", "_saved_preview"),
    ("GET", "/spool/{pid}", "_spool_image"),
    ("GET", "/timelapse/{rid}", "_timelapse_file"),
    ("POST", "/api/admin/ai", "_api_admin_ai_action"),
    ("POST", "/api/admin/ai_test", "_api_admin_ai_test"),
    ("POST", "/api/admin/backup_now", "_api_admin_backup_now"),
    ("POST", "/api/admin/balance", "_api_admin_balance"),
    ("POST", "/api/admin/balance_comment", "_api_admin_balance_comment"),
    ("POST", "/api/admin/batch_calc", "_api_admin_batch_calc"),
    ("POST", "/api/admin/batch_cancel", "_api_admin_batch_cancel"),
    ("POST", "/api/admin/batch_send", "_api_admin_batch_send"),
    ("POST", "/api/admin/brand_logo", "_api_admin_brand_logo"),
    ("POST", "/api/admin/broadcast", "_api_admin_broadcast"),
    ("POST", "/api/admin/calc_queue_resolve", "_api_admin_calc_queue_resolve"),
    ("POST", "/api/admin/calib/backfill", "_api_admin_calib_backfill"),
    ("POST", "/api/admin/calib/link", "_api_admin_calib_link"),
    ("POST", "/api/admin/calib/measure_add", "_api_admin_calib_measure_add"),
    ("POST", "/api/admin/calib/measure_delete", "_api_admin_calib_measure_delete"),
    ("POST", "/api/admin/calib/passport_apply", "_api_admin_calib_passport_apply"),
    ("POST", "/api/admin/calib/passport_delete", "_api_admin_calib_passport_delete"),
    ("POST", "/api/admin/calib/passport_save", "_api_admin_calib_passport_save"),
    ("POST", "/api/admin/calib/photo", "_api_admin_calib_photo"),
    ("POST", "/api/admin/calib/plate_save", "_api_admin_calib_plate_save"),
    ("POST", "/api/admin/calib/preset_restore", "_api_admin_calib_preset_restore"),
    ("POST", "/api/admin/calib/presets_snapshot", "_api_admin_calib_presets_snapshot"),
    ("POST", "/api/admin/calib/run_delete", "_api_admin_calib_run_delete"),
    ("POST", "/api/admin/calib/run_save", "_api_admin_calib_run_save"),
    ("POST", "/api/admin/calib/sample_delete", "_api_admin_calib_sample_delete"),
    ("POST", "/api/admin/calib/sample_save", "_api_admin_calib_sample_save"),
    ("POST", "/api/admin/calib/step_apply", "_api_admin_calib_step_apply"),
    ("POST", "/api/admin/calib/step_save", "_api_admin_calib_step_save"),
    ("POST", "/api/admin/client_discount", "_api_admin_client_discount"),
    ("POST", "/api/admin/client_message", "_api_admin_client_message"),
    ("POST", "/api/admin/client_save", "_api_admin_client_save"),
    ("POST", "/api/admin/collection", "_api_admin_collection_edit"),
    ("POST", "/api/admin/delivery_type", "_api_admin_delivery_type"),
    ("POST", "/api/admin/discount", "_api_admin_discount"),
    ("POST", "/api/admin/errors/seen", "_api_admin_errors_seen"),
    ("POST", "/api/admin/family_add", "_api_admin_family_add"),
    ("POST", "/api/admin/family_create", "_api_admin_family_create"),
    ("POST", "/api/admin/family_remove", "_api_admin_family_remove"),
    ("POST", "/api/admin/feature", "_api_admin_feature"),
    ("POST", "/api/admin/journal_add", "_api_admin_journal_add"),
    ("POST", "/api/admin/journal_delete", "_api_admin_journal_delete"),
    ("POST", "/api/admin/journal_media_delete", "_api_admin_journal_media_delete"),
    ("POST", "/api/admin/journal_update", "_api_admin_journal_update"),
    ("POST", "/api/admin/kb/delete", "_api_admin_kb_delete"),
    ("POST", "/api/admin/kb/save", "_api_admin_kb_save"),
    ("POST", "/api/admin/mw_account", "_api_admin_mw_account"),
    ("POST", "/api/admin/notification_action", "_api_admin_notification_action"),
    ("POST", "/api/admin/option", "_api_admin_option"),
    ("POST", "/api/admin/order/attach_print", "_api_admin_order_attach_print"),
    ("POST", "/api/admin/order/bill", "_api_admin_order_bill"),
    ("POST", "/api/admin/order/create", "_api_admin_order_create"),
    ("POST", "/api/admin/order/delete", "_api_admin_order_delete"),
    ("POST", "/api/admin/order/delivery", "_api_admin_order_delivery"),
    ("POST", "/api/admin/order/invoice", "_api_admin_order_invoice"),
    ("POST", "/api/admin/order/material", "_api_admin_order_material"),
    ("POST", "/api/admin/order/message", "_api_admin_order_message"),
    ("POST", "/api/admin/order/open_folder", "_api_admin_order_open_folder"),
    ("POST", "/api/admin/order/open_slicer", "_api_admin_order_open_slicer"),
    ("POST", "/api/admin/order/paid", "_api_admin_order_paid"),
    ("POST", "/api/admin/order/price", "_api_admin_order_price"),
    ("POST", "/api/admin/order/print_net", "_api_admin_order_print_net"),
    ("POST", "/api/admin/order/recalc", "_api_admin_order_recalc"),
    ("POST", "/api/admin/order/recalc_apply", "_api_admin_order_recalc_apply"),
    ("POST", "/api/admin/order/shipment", "_api_admin_order_shipment"),
    ("POST", "/api/admin/order/slice", "_api_admin_order_slice"),
    ("POST", "/api/admin/order/status", "_api_admin_order_status"),
    ("POST", "/api/admin/order_media_delete", "_api_admin_order_media_delete"),
    ("POST", "/api/admin/orders/clean_test", "_api_admin_orders_clean_test"),
    ("POST", "/api/admin/orders/mark_all_seen", "_api_admin_orders_mark_all_seen"),
    ("POST", "/api/admin/photo", "_api_admin_photo"),
    ("POST", "/api/admin/plastic", "_api_admin_plastic"),
    ("POST", "/api/admin/preset", "_api_admin_preset"),
    ("POST", "/api/admin/price_calc", "_api_admin_price_calc"),
    ("POST", "/api/admin/print_classify", "_api_admin_print_classify_pick"),
    ("POST", "/api/admin/print_confirm", "_api_admin_print_confirm"),
    ("POST", "/api/admin/printer", "_api_admin_printer"),
    ("POST", "/api/admin/printer_bind", "_api_admin_printer_bind"),
    ("POST", "/api/admin/printer_control", "_api_admin_printer_control"),
    ("POST", "/api/admin/printer_delete", "_api_admin_printer_delete"),
    ("POST", "/api/admin/printer_fan", "_api_admin_printer_fan"),
    ("POST", "/api/admin/printer_heat", "_api_admin_printer_heat"),
    ("POST", "/api/admin/printer_home", "_api_admin_printer_home"),
    ("POST", "/api/admin/printer_load", "_api_admin_printer_load"),
    ("POST", "/api/admin/printer_move", "_api_admin_printer_move"),
    ("POST", "/api/admin/printer_print", "_api_admin_printer_print"),
    ("POST", "/api/admin/printer_unbind", "_api_admin_printer_unbind"),
    ("POST", "/api/admin/printer_unload", "_api_admin_printer_unload"),
    ("POST", "/api/admin/printer_upload", "_api_admin_printer_upload"),
    ("POST", "/api/admin/product/delete", "_api_admin_product_delete"),
    ("POST", "/api/admin/product/save", "_api_admin_product_save"),
    ("POST", "/api/admin/promo", "_api_admin_promo"),
    ("POST", "/api/admin/reprint", "_api_admin_reprint"),
    ("POST", "/api/admin/reprint_start", "_api_admin_reprint_start"),
    # «Инструменты» → допечатка со слоя (Путь В, app/web/tools.py)
    ("POST", "/api/admin/tools/resume/upload", "_api_tools_resume_upload"),
    ("POST", "/api/admin/tools/resume/analyze", "_api_tools_resume_analyze"),
    ("POST", "/api/admin/tools/resume/build", "_api_tools_resume_build"),
    ("POST", "/api/admin/tools/resume/send", "_api_tools_resume_send"),
    ("POST", "/api/admin/tools/resume/start", "_api_tools_resume_start"),
    # Пульт, вторая очередь (app/web/printerext.py)
    ("POST", "/api/admin/heat_presets", "_api_heat_presets_set"),
    ("POST", "/api/admin/printer_community", "_api_community_post"),
    ("POST", "/api/admin/printer_off", "_api_printer_off"),
    ("POST", "/api/admin/printer_refill", "_api_printer_refill"),
    ("POST", "/api/admin/printer_speed", "_api_printer_speed"),
    ("POST", "/api/admin/queue/move", "_api_admin_queue_move"),
    ("POST", "/api/admin/screen_power", "_api_admin_screen_power"),
    ("POST", "/api/admin/self_print", "_api_admin_self_print"),
    ("POST", "/api/admin/service", "_api_admin_service_action"),
    ("POST", "/api/admin/setting", "_api_admin_setting"),
    ("POST", "/api/admin/shade", "_api_admin_shade"),
    ("POST", "/api/admin/spool", "_api_admin_spool"),
    ("POST", "/api/admin/staff", "_api_admin_staff_save"),
    ("POST", "/api/admin/task", "_api_admin_task"),
    ("POST", "/api/admin/update_apply", "_api_admin_update_apply"),
    ("POST", "/api/admin/update_check", "_api_admin_update_check"),
    ("POST", "/api/admin/update_mode", "_api_admin_update_mode"),
    ("POST", "/api/avatar/generate", "_api_avatar_generate"),
    ("POST", "/api/calc", "_api_calc"),
    ("POST", "/api/calc_notify", "_api_calc_notify"),
    ("POST", "/api/calc_queue", "_api_calc_queue"),
    ("POST", "/api/cart_sync", "_api_cart_sync"),
    ("POST", "/api/colors_sel", "_api_colors_sel"),
    ("POST", "/api/custom_order", "_api_custom_order"),
    ("POST", "/api/desk/login", "_api_desk_login"),
    ("POST", "/api/desk/logout", "_api_desk_logout"),
    ("POST", "/api/family_pick_partner", "_api_family_pick_partner"),
    ("POST", "/api/family_request", "_api_family_request"),
    ("POST", "/api/favorite", "_api_favorite"),
    ("POST", "/api/file_variant", "_api_file_variant"),
    ("POST", "/api/idea", "_api_idea"),
    ("POST", "/api/jserr", "_api_jserr"),
    ("POST", "/api/model", "_api_model"),
    ("POST", "/api/order", "_api_order"),
    ("POST", "/api/order/cancel", "_api_order_cancel"),
    ("POST", "/api/order/edit", "_api_order_edit"),
    ("POST", "/api/order/message", "_api_order_message"),
    ("POST", "/api/order/pay_link", "_api_order_pay_link"),
    ("POST", "/api/order/pdf", "_api_order_pdf"),
    ("POST", "/api/order/respond", "_api_order_respond"),
    ("POST", "/api/order_cart", "_api_order_cart"),
    ("POST", "/api/order_now", "_api_order_now"),
    ("POST", "/api/order_unsliced", "_api_order_unsliced"),
    ("POST", "/api/parametric/model", "_api_parametric_model"),
    ("POST", "/api/parametric/preview2c", "_api_parametric_preview2c"),
    ("POST", "/api/pdf", "_api_pdf"),
    ("POST", "/api/prefs", "_api_prefs"),
    ("POST", "/api/product_order", "_api_product_order"),
    ("POST", "/api/requote", "_api_requote"),
    ("POST", "/api/use", "_api_use"),
    ("POST", "/api/variant", "_api_variant"),
    ("POST", "/pick", "_pick"),
    ("POST", "/pickplastic", "_pick_plastic"),
})

# ─────────────────────────── эталон: карта прав админки ───────────────────────────
# Право, которое _caps_mw требует на входе в ручку. Ключи — ВСЕ пути, которые эта middleware
# сторожит (/api/admin/… плюс /admin/…), а не только те, что названы в _OPERATOR_ROUTES /
# _OWNER_ROUTES: остальные попадают в «закрытое по умолчанию» (roles.CAP_SETTINGS = владелец),
# и именно эту умолчальную закрытость снимок и фиксирует.
#
# Снято 08.08.2026 после сужения прав у /api/admin/dashboard (finance) — оператору он больше
# не отдаётся. Обновлено 09.08.2026: /api/admin/plastics ВЕРНУЛСЯ оператору под право принтера —
# справочник материалов нужен ему в карточке заказа и в журнале печати, а цена за кг и коэффициент
# сложности режутся внутри самой ручки.
ADMIN_CAPS: dict[str, str] = {
    "/admin/photo/{kind}/{id}":             roles.CAP_ORDERS,
    "/api/admin/activity":                  roles.CAP_CLIENTS,
    "/api/admin/ai":                        roles.CAP_SETTINGS,
    "/api/admin/ai_test":                   roles.CAP_SETTINGS,
    "/api/admin/backup_now":                roles.CAP_SETTINGS,
    "/api/admin/backup_status":             roles.CAP_SETTINGS,
    "/api/admin/balance":                   roles.CAP_FINANCE,
    "/api/admin/balance_comment":           roles.CAP_FINANCE,
    "/api/admin/batch_calc":                roles.CAP_FINANCE,
    "/api/admin/batch_cancel":              roles.CAP_SETTINGS,
    "/api/admin/batch_last":                roles.CAP_SETTINGS,
    "/api/admin/batch_send":                roles.CAP_FINANCE,
    "/api/admin/batch_status":              roles.CAP_SETTINGS,
    "/api/admin/bigdata":                   roles.CAP_FINANCE,
    "/api/admin/brand_logo":                roles.CAP_SETTINGS,
    "/api/admin/broadcast":                 roles.CAP_CLIENTS,
    "/api/admin/calc_queue":                roles.CAP_SETTINGS,
    "/api/admin/calc_queue_resolve":        roles.CAP_SETTINGS,
    "/api/admin/calib":                     roles.CAP_PRINTER,
    "/api/admin/calib/backfill":            roles.CAP_PRINTER,
    "/api/admin/calib/drift":               roles.CAP_PRINTER,
    "/api/admin/calib/export":              roles.CAP_PRINTER,
    "/api/admin/calib/labels":              roles.CAP_PRINTER,
    "/api/admin/calib/link":                roles.CAP_PRINTER,
    "/api/admin/calib/measure_add":         roles.CAP_PRINTER,
    "/api/admin/calib/measure_delete":      roles.CAP_PRINTER,
    "/api/admin/calib/passport_apply":      roles.CAP_PRINTER,
    "/api/admin/calib/passport_delete":     roles.CAP_PRINTER,
    "/api/admin/calib/passport_save":       roles.CAP_PRINTER,
    "/api/admin/calib/photo":               roles.CAP_PRINTER,
    "/api/admin/calib/plate_save":          roles.CAP_PRINTER,
    "/api/admin/calib/preset_history":      roles.CAP_PRINTER,
    "/api/admin/calib/preset_restore":      roles.CAP_PRINTER,
    "/api/admin/calib/presets":             roles.CAP_PRINTER,
    "/api/admin/calib/presets_snapshot":    roles.CAP_PRINTER,
    "/api/admin/calib/run":                 roles.CAP_PRINTER,
    "/api/admin/calib/run_delete":          roles.CAP_PRINTER,
    "/api/admin/calib/run_save":            roles.CAP_PRINTER,
    "/api/admin/calib/sample_delete":       roles.CAP_PRINTER,
    "/api/admin/calib/sample_save":         roles.CAP_PRINTER,
    "/api/admin/calib/step_apply":          roles.CAP_PRINTER,
    "/api/admin/calib/step_save":           roles.CAP_PRINTER,
    "/api/admin/camera":                    roles.CAP_PRINTER,
    "/api/admin/camera_check":              roles.CAP_PRINTER,
    "/api/admin/chat_search":               roles.CAP_SETTINGS,
    "/api/admin/calc":                      roles.CAP_CLIENTS,
    "/api/admin/client":                    roles.CAP_CLIENTS,
    "/api/admin/client_chat":               roles.CAP_CLIENTS,
    "/api/admin/client_discount":           roles.CAP_CLIENTS,
    "/api/admin/client_message":            roles.CAP_CLIENTS,
    "/api/admin/client_save":               roles.CAP_CLIENTS,
    "/api/admin/clients":                   roles.CAP_CLIENTS,
    "/api/admin/clients/export.csv":        roles.CAP_CLIENTS,
    "/api/admin/collection":                roles.CAP_SETTINGS,
    "/api/admin/dashboard":                 roles.CAP_FINANCE,
    "/api/admin/delivery":                  roles.CAP_PRICING,
    "/api/admin/delivery_type":             roles.CAP_PRICING,
    "/api/admin/discount":                  roles.CAP_PRICING,
    "/api/admin/discounts":                 roles.CAP_PRICING,
    "/api/admin/errors":                    roles.CAP_SETTINGS,
    "/api/admin/errors/seen":               roles.CAP_SETTINGS,
    "/api/admin/events":                    roles.CAP_ORDERS,
    "/api/admin/families":                  roles.CAP_CLIENTS,
    "/api/admin/family_add":                roles.CAP_SETTINGS,
    "/api/admin/family_create":             roles.CAP_SETTINGS,
    "/api/admin/family_remove":             roles.CAP_SETTINGS,
    "/api/admin/feature":                   roles.CAP_SETTINGS,
    "/api/admin/hub":                       roles.CAP_ORDERS,
    "/api/admin/journal":                   roles.CAP_PRINTER,
    "/api/admin/journal_add":               roles.CAP_PRINTER,
    "/api/admin/journal_delete":            roles.CAP_PRINTER,
    "/api/admin/journal_media_delete":      roles.CAP_PRINTER,
    "/api/admin/journal_update":            roles.CAP_PRINTER,
    "/api/admin/kb":                        roles.CAP_SETTINGS,
    "/api/admin/kb/delete":                 roles.CAP_SETTINGS,
    "/api/admin/kb/save":                   roles.CAP_SETTINGS,
    "/api/admin/mw_account":                roles.CAP_SETTINGS,
    "/api/admin/mw_accounts":               roles.CAP_SETTINGS,
    "/api/admin/notification_action":       roles.CAP_SETTINGS,
    "/api/admin/notifications":             roles.CAP_SETTINGS,
    "/api/admin/option":                    roles.CAP_PRICING,
    "/api/admin/options":                   roles.CAP_PRICING,
    "/api/admin/order/attach_print":        roles.CAP_ORDERS,
    "/api/admin/order/bill":                roles.CAP_FINANCE,
    "/api/admin/order/create":              roles.CAP_FINANCE,
    "/api/admin/order/delete":              roles.CAP_SETTINGS,
    "/api/admin/order/delivery":            roles.CAP_FINANCE,
    "/api/admin/order/invoice":             roles.CAP_FINANCE,
    "/api/admin/order/material":            roles.CAP_ORDERS,
    "/api/admin/order/message":             roles.CAP_ORDERS,
    "/api/admin/order/open_folder":         roles.CAP_ORDERS,
    "/api/admin/order/open_slicer":         roles.CAP_ORDERS,
    "/api/admin/order/paid":                roles.CAP_FINANCE,
    "/api/admin/order/price":               roles.CAP_FINANCE,
    "/api/admin/order/print_net":           roles.CAP_ORDERS,
    "/api/admin/order/recalc":              roles.CAP_FINANCE,
    "/api/admin/order/recalc_apply":        roles.CAP_FINANCE,
    "/api/admin/order/shipment":            roles.CAP_ORDERS,
    "/api/admin/order/slice":               roles.CAP_ORDERS,
    "/api/admin/order/status":              roles.CAP_ORDERS,
    "/api/admin/gridfinity/{rid}":          roles.CAP_ORDERS,
    "/api/admin/gridfinity/part/{id}":      roles.CAP_ORDERS,
    "/api/admin/gridfinity/release/{rid}":  roles.CAP_ORDERS,
    "/api/admin/order/{id}":                roles.CAP_ORDERS,
    "/api/admin/order_media_delete":        roles.CAP_ORDERS,
    "/api/admin/orderfile":                 roles.CAP_ORDERS,
    "/api/admin/orders":                    roles.CAP_ORDERS,
    "/api/admin/orders/clean_test":         roles.CAP_SETTINGS,
    "/api/admin/orders/export.csv":         roles.CAP_FINANCE,
    "/api/admin/orders/mark_all_seen":      roles.CAP_ORDERS,
    "/api/admin/photo":                     roles.CAP_ORDERS,
    "/api/admin/plastic":                   roles.CAP_PRICING,
    "/api/admin/plastics":                  roles.CAP_PRINTER,
    "/api/admin/preset":                    roles.CAP_SETTINGS,
    "/api/admin/presets":                   roles.CAP_SETTINGS,
    "/api/admin/price_calc":                roles.CAP_FINANCE,
    "/api/admin/print_classify":            roles.CAP_PRINTER,
    "/api/admin/print_confirm":             roles.CAP_PRINTER,
    "/api/admin/printer":                   roles.CAP_SETTINGS,
    "/api/admin/printer_bind":              roles.CAP_PRINTER,
    "/api/admin/printer_control":           roles.CAP_PRINTER,
    "/api/admin/printer_delete":            roles.CAP_PRINTER,
    "/api/admin/printer_fan":               roles.CAP_PRINTER,
    "/api/admin/printer_files":             roles.CAP_PRINTER,
    "/api/admin/printer_heat":              roles.CAP_PRINTER,
    "/api/admin/printer_history":           roles.CAP_PRINTER,
    "/api/admin/printer_home":              roles.CAP_PRINTER,
    "/api/admin/printer_load":              roles.CAP_PRINTER,
    "/api/admin/printer_move":              roles.CAP_PRINTER,
    "/api/admin/printer_print":             roles.CAP_PRINTER,
    "/api/admin/printer_status":            roles.CAP_PRINTER,
    "/api/admin/printer_unbind":            roles.CAP_PRINTER,
    "/api/admin/printer_unload":            roles.CAP_PRINTER,
    "/api/admin/printer_upload":            roles.CAP_PRINTER,
    "/api/admin/printers":                  roles.CAP_PRINTER,
    "/api/admin/reprint":                   roles.CAP_PRINTER,
    "/api/admin/reprint_start":             roles.CAP_PRINTER,
    "/api/admin/reprint_parts":             roles.CAP_PRINTER,
    # «Инструменты» → допечатка со слоя (Путь В): за ручками заливка на принтер и старт
    "/api/admin/tools/resume/sources":      roles.CAP_PRINTER,
    "/api/admin/tools/resume/upload":       roles.CAP_PRINTER,
    "/api/admin/tools/resume/analyze":      roles.CAP_PRINTER,
    "/api/admin/tools/resume/build":        roles.CAP_PRINTER,
    "/api/admin/tools/resume/download":     roles.CAP_PRINTER,
    "/api/admin/tools/resume/send":         roles.CAP_PRINTER,
    "/api/admin/tools/resume/start":        roles.CAP_PRINTER,
    "/api/admin/tools/resume/job":          roles.CAP_PRINTER,
    "/api/admin/tools/resume/paths":        roles.CAP_PRINTER,
    "/api/admin/tools/resume/gates":        roles.CAP_PRINTER,
    "/api/admin/tools/resume/history":      roles.CAP_PRINTER,
    "/api/admin/printer_file_info":         roles.CAP_PRINTER,
    "/api/admin/printer_thumb":             roles.CAP_PRINTER,
    "/api/admin/printer_speed":             roles.CAP_PRINTER,
    "/api/admin/printer_refill":            roles.CAP_PRINTER,
    "/api/admin/printer_off":               roles.CAP_PRINTER,
    "/api/admin/heat_presets":              roles.CAP_PRINTER,
    "/api/admin/printer_community":         roles.CAP_PRINTER,
    "/api/admin/product/delete":            roles.CAP_PRICING,
    "/api/admin/product/save":              roles.CAP_PRICING,
    "/api/admin/products":                  roles.CAP_PRICING,
    "/api/admin/promo":                     roles.CAP_PRICING,
    "/api/admin/promos":                    roles.CAP_PRICING,
    "/api/admin/queue":                     roles.CAP_ORDERS,
    "/api/admin/queue/move":                roles.CAP_ORDERS,
    "/api/admin/screen_power":              roles.CAP_PRINTER,
    "/api/admin/self_orders":               roles.CAP_SETTINGS,
    "/api/admin/self_print":                roles.CAP_SETTINGS,
    "/api/admin/service":                   roles.CAP_PRINTER,
    "/api/admin/setting":                   roles.CAP_SETTINGS,
    "/api/admin/settings":                  roles.CAP_SETTINGS,
    "/api/admin/shade":                     roles.CAP_SETTINGS,
    "/api/admin/shades":                    roles.CAP_SETTINGS,
    "/api/admin/spool":                     roles.CAP_PRICING,
    "/api/admin/spools":                    roles.CAP_PRINTER,
    "/api/admin/staff":                     roles.CAP_SETTINGS,
    "/api/admin/status":                    roles.CAP_ORDERS,
    "/api/admin/task":                      roles.CAP_ORDERS,
    "/api/admin/tasks":                     roles.CAP_ORDERS,
    "/api/admin/update_apply":              roles.CAP_SETTINGS,
    "/api/admin/update_check":              roles.CAP_SETTINGS,
    "/api/admin/update_mode":               roles.CAP_SETTINGS,
    "/api/admin/usage":                     roles.CAP_SETTINGS,
}


# ─────────────────────────── разбор расхождений ───────────────────────────

def _routes_diff(actual: frozenset[tuple[str, str, str]],
                 expected: frozenset[tuple[str, str, str]]) -> str:
    """Разница двух таблиц маршрутов словами. Пустая строка — таблицы совпали."""
    # Раскладываем тройки в «путь → обработчик»: подмену видно только при сравнении ПО ПУТИ,
    # разность множеств троек показала бы её как пропажу плюс появление — а на такую пару
    # ревьюер по инструкции выше просто вставляет новые строки в эталон и подмену узаконивает.
    act: dict[tuple[str, str], str] = {(m, c): h for m, c, h in actual}
    exp: dict[tuple[str, str], str] = {(m, c): h for m, c, h in expected}
    lost = sorted(set(exp) - set(act))
    swapped = sorted(k for k in set(act) & set(exp) if act[k] != exp[k])
    added = sorted(set(act) - set(exp))
    lines: list[str] = []
    if lost:
        lines.append(f"ПРОПАЛИ маршруты ({len(lost)}) — ручка потеряна, чините код:")
        lines += [f"    {m} {c}" for m, c in lost]
    if swapped:
        lines.append(f"ПУТЬ ОБСЛУЖИВАЕТ ДРУГАЯ РУЧКА ({len(swapped)}) — "
                     "путь на месте, отвечает другая функция:")
        lines += [f"    {m} {c} — было «{exp[(m, c)]}», стало «{act[(m, c)]}»" for m, c in swapped]
    if added:
        lines.append(f"ПОЯВИЛИСЬ маршруты ({len(added)}) — если это законно, вставьте в ROUTES:")
        lines += [f'    ("{m}", "{c}", "{act[(m, c)]}"),' for m, c in added]
    return "\n".join(lines)


# Право → имя константы: подсказка о новом пути печатается в том же виде, в каком её
# вставлять в ADMIN_CAPS (roles.CAP_ORDERS, а не «orders»).
_CAP_CONST: dict[str, str] = {getattr(roles, n): f"roles.{n}" for n in dir(roles)
                              if n.startswith("CAP_") and isinstance(getattr(roles, n), str)}


def _caps_diff(actual: dict[str, str], expected: dict[str, str]) -> str:
    """Разница двух карт прав словами. Пустая строка — карты совпали."""
    lines: list[str] = []
    for path in sorted(set(expected) - set(actual)):
        lines.append(f"    ПРОПАЛ путь {path} (был под правом «{expected[path]}»)")
    for path in sorted(set(actual) - set(expected)):
        cap = _CAP_CONST.get(actual[path], repr(actual[path]))
        lines.append(f'    "{path}": {cap},   ← новый путь, вставьте в ADMIN_CAPS')
    for path in sorted(set(actual) & set(expected)):
        if actual[path] != expected[path]:
            lines.append(f"    ПРАВО ИЗМЕНИЛОСЬ у {path}: "
                         f"было «{expected[path]}», стало «{actual[path]}»")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def routes() -> frozenset[tuple[str, str, str]]:
    """Настоящая таблица маршрутов приложения — тройки (метод, канонический путь, имя ручки).

    make_app() ничего не открывает и никуда не ходит (см. tests/test_kb.py), но и звать его на
    каждый тест незачем — отсюда область module."""
    app = webapp.make_app()
    rows: list[tuple[str, str, str]] = []
    for route in app.router.routes():
        res = getattr(route, "resource", None)
        # Ресурса нет только у роутов, заведённых в обход add_get/add_post; таких быть не должно —
        # иначе маршрут выпал бы из снимка молча, ровно от чего этот файл и страхует.
        assert res is not None, f"маршрут без ресурса: {route!r}"
        rows.append((route.method, res.canonical, route.handler.__qualname__))
    # Дубли ищем по ПАРЕ, а не по тройке: две ручки на одном пути — это и есть дубль, а по
    # тройкам он выглядел бы как два разных элемента и проверка молчала бы.
    pairs = [(m, c) for m, c, _h in rows]
    assert len(pairs) == len(set(pairs)), "в таблице маршрутов появились дубли"
    return frozenset(rows)


# ─────────────────────────── сами проверки ───────────────────────────

def test_route_table_matches_snapshot(routes):
    """Полная таблица GET/POST совпадает с эталоном — ни одна ручка не пропала, не появилась
    и не подменилась."""
    actual = frozenset(r for r in routes if r[0] != "HEAD")
    diff = _routes_diff(actual, ROUTES)
    assert not diff, "таблица маршрутов разошлась с эталоном:\n" + diff


def test_head_mirrors_get(routes):
    """HEAD заводит сам aiohttp к каждому GET — потому его и нет в эталоне.

    Проверка держит это допущение: перестанет выполняться (кто-то добавит GET с allow_head=False
    или заведёт HEAD руками) — снимок про HEAD станет неполным, и мы об этом узнаем. Сверяем
    вместе с обработчиком: HEAD, отданный чужой функции, тоже прошёл бы мимо эталона."""
    heads = {(c, h) for m, c, h in routes if m == "HEAD"}
    gets = {(c, h) for m, c, h in routes if m == "GET"}
    assert heads == gets, ("HEAD больше не зеркалит GET, различия: "
                           f"только HEAD={sorted(heads - gets)}, только GET={sorted(gets - heads)}")


def test_admin_route_caps_match_snapshot(routes):
    """Карта прав команды (A4) для КАЖДОГО админского пути — снимок _route_cap.

    Ловит и потерю ручки из карты, и тихое расширение доступа: ручка, выпавшая из
    _OPERATOR_ROUTES, не ломается — она просто становится владельческой, и оператор молча
    теряет кнопку. Обратное опаснее: денежная ручка, случайно попавшая в операторский список,
    открывает оператору выручку."""
    admin = sorted({c for _m, c, _h in routes
                    # ровно то условие, по которому _caps_mw решает, сторожить ли запрос
                    if c.startswith("/api/admin/") or c.startswith("/admin/")})
    actual = {c: webapp._route_cap(c) for c in admin}
    diff = _caps_diff(actual, ADMIN_CAPS)
    assert not diff, "карта прав админки разошлась с эталоном:\n" + diff


def test_admin_caps_use_known_capabilities():
    """Каждое право из снимка — настоящее право из roles.ALL_CAPS.

    Опечатка в названии («printers» вместо «printer») не роняет ничего: caps_of() просто никогда
    не содержит такой строки, и ручка тихо закрывается для ВСЕХ, включая владельца."""
    unknown = sorted({cap for cap in ADMIN_CAPS.values() if cap not in roles.ALL_CAPS})
    assert not unknown, f"в карте прав неизвестные права: {unknown}"


# ─────────────────────────── сторож самого сторожа ───────────────────────────
# Снимок, который не краснеет на поломке, бесполезен: ниже — проверки, что разбор расхождений
# действительно ловит и пропажу, и подмену, а не молча возвращает пустую строку. Подмена здесь
# опаснее: она НИЧЕГО не меняет в множестве путей, поэтому без своей категории разбор молчит
# ровно так же, как молчал старый снимок из пар.

def test_diff_reports_lost_route(routes):
    # Пару, а не тройку: тест не должен знать имя обработчика — оно живёт в эталоне.
    broken = frozenset(r for r in routes if r[0] != "HEAD" and r[:2] != ("POST", "/api/order"))
    msg = _routes_diff(broken, ROUTES)
    assert "ПРОПАЛИ" in msg and "POST /api/order" in msg


def test_diff_reports_swapped_handler():
    """Свап обработчиков — та самая дыра, ради которой снимок стал тройками.

    Пути при свапе целы оба, поэтому снимок из пар молчал бы. И мало сказать «разошлось»:
    в разности множеств свап выглядит как пропажа плюс появление, а на такую пару инструкция
    в шапке велит вставить новые строки в эталон — то есть подмену бы узаконили. Проверяем
    именно то, что разбор называет её отдельной категорией и НЕ печатает двух других.

    Ломаем эталон, а не живую таблицу: если однажды эти две ручки и правда поменяют местами
    в коде, второй свап отменил бы первый, сторож замолчал бы — и в прогоне осталась бы одна
    невнятная красная строка вместо двух внятных."""
    a, b = ("GET", "/api/admin/calib/drift"), ("GET", "/api/admin/calib/labels")
    ref = {(m, c): h for m, c, h in ROUTES}
    assert a in ref and b in ref, "образцовые ручки исчезли из эталона, возьмите другую пару"
    broken = dict(ref)
    broken[a], broken[b] = ref[b], ref[a]
    msg = _routes_diff(frozenset((m, c, h) for (m, c), h in broken.items()), ROUTES)
    assert "ПУТЬ ОБСЛУЖИВАЕТ ДРУГАЯ РУЧКА (2)" in msg, msg
    assert f"GET /api/admin/calib/drift — было «{ref[a]}», стало «{ref[b]}»" in msg, msg
    assert f"GET /api/admin/calib/labels — было «{ref[b]}», стало «{ref[a]}»" in msg, msg
    assert "ПРОПАЛИ" not in msg and "ПОЯВИЛИСЬ" not in msg, msg


def test_diff_reports_widened_cap():
    widened = dict(ADMIN_CAPS, **{"/api/admin/dashboard": roles.CAP_ORDERS})
    msg = _caps_diff(widened, ADMIN_CAPS)
    assert "/api/admin/dashboard" in msg and "ПРАВО ИЗМЕНИЛОСЬ" in msg
