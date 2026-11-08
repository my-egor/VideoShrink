# VideoShrink Quality and Size Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Реализовать два режима VideoShrink: проверяемое сжатие без потери кадров и адаптивное сжатие с контролем фактического размера и минимальной разумной потерей качества.

**Architecture:** Сначала зафиксировать исходные метрики на том же контрольном ролике. В `videoshrink/core.py` централизовать проверки размера, точное сравнение кадров и финальное кодирование; отдельный `videoshrink/quality_search.py` выбирает CRF по выборочным фрагментам через совместимый `ab-av1` или его sample-based алгоритм на уже включённых FFmpeg/x265/VMAF. В `videoshrink/app.py` синхронизировать настройки, очередь, историю, этапы и ETA; затем собрать и проверить портативную версию.

**Tech Stack:** Python 3.12, Tkinter, bundled FFmpeg/ffprobe 9.0.2, libx265, libvmaf, unittest, PyInstaller, PowerShell.

**Spec:** `docs/superpowers/specs/2026-11-07-video-quality-size-policy-design.md`

## Global Constraints

- Режим по умолчанию — `lossless`; кодирование начинается только после явного нажатия кнопки.
- Strict принимает файл только если он меньше источника и последовательности хешей декодированных видеокадров полностью совпали. Лимит 2 000 000 000 байт к strict не применяется.
- Adaptive делает ровно одну lossless-попытку; если её результат не подходит, запускает lossy fallback в том числе для входа меньше 2 ГБ.
- В обоих режимах результат всегда строго меньше источника. Для adaptive входа, достигшего эффективного лимита, результат также должен быть строго меньше лимита; абсолютный максимум — 2 000 000 000 байт.
- Для входа меньше лимита цель поиска меньше размера входа; по умолчанию для входа от 2 ГБ целиться в 1 900 000 000 байт. Точные байты проверяются после полного кодирования.
- Порог VMAF по умолчанию 95 — целевое значение. Если он несовместим с размером, выбрать самый качественный помещающийся кандидат и явно сообщить о пороге, который не достигнут.
- Не изменять оригинал. Публиковать только полностью проверенный кандидат; при отмене/ошибке удалять временные файлы текущей задачи.
- Сохранять совместимые дорожки и метаданные. Невозможность сохранить HDR, субтитры, вложения, дополнительные видео или иные потоки должна завершать задачу понятной ошибкой, без молчаливого пропуска.
- Настройки и главный экран используют одно сохранённое значение режима. Предупреждение показывать при явном выборе adaptive, не блокировать каждый запуск.
- Общий процент, этап, пульсация заполненной части шкалы и ETA включают sample search, полный encode и проверку. Диагностика фиксирует этап, длительности, режим, параметры попытки, размер кандидата, код/причину ошибки; не записывает содержимое видео.
- Сначала проверить официальный Windows-релиз `ab-av1 v0.11.7` (найден 2026-11-08) с текущими FFmpeg/libx265/libvmaf. Не требовать установленный пользователем Python/FFmpeg и не заменять FFmpeg ради SVT-AV1. Если инструмент несовместим, использовать его sample-based подход поверх текущей сборки. При включении бинарника закрепить версию, SHA-256 и MIT-уведомление.
- Обновить версию с 1.1.5 до 1.1.6. В проекте нет `.git`; не создавать коммиты и изменять файлы только в VideoShrink.

## Review Focus

- Lossless-выход может превышать 2 ГБ, но быть меньше большего источника: strict должен принять его только при точном совпадении кадров. Тесты: `test_strict_lossless_accepts_smaller_than_source_above_two_gib` и `test_strict_rejects_mismatched_decoded_frame_hashes`.
- Малый источник не уменьшается без потерь: adaptive обязан перейти к lossy, не завершившись раньше. Тест: `test_adaptive_falls_back_for_source_below_two_gib`.
- Выход ровно на границе лимита не является файлом «меньше лимита». Тест: `test_adaptive_requires_strictly_below_limit_at_boundary`.
- Ни один sample не достигает VMAF-порога, но подходящий по размеру кандидат есть: выбрать лучший из подходящих и поставить предупреждающий флаг. Тест: `test_search_returns_best_fitting_candidate_and_marks_quality_warning`.
- Отмена, ошибка кодировщика или несовместимая дорожка не должны публиковать partial и менять оригинал. Тесты: `test_cancel_removes_only_current_partial` и `test_unsupported_stream_fails_without_publishing_output`.

## File Structure

- Изменить `videoshrink/core.py`: предикат результата, frame hashes, lossless/adaptive переходы, CRF encode и полная валидация.
- Создать `videoshrink/quality_search.py`: выбор CRF, sample VMAF, прогноз размера/времени, отмена и прогресс.
- Изменить `videoshrink/app.py`: выбор режима в настройках и на главном экране, предупреждение, история/очередь, прогресс и ETA.
- Изменить `tests/test_core.py`; создать `tests/test_quality_search.py` и `tests/test_app_workflow.py`.
- Изменить `build.ps1`, `VideoShrink.spec`, `installer.nsi`, `installer.iss`, `README.md` и `licenses/` только для версии/фактически поставляемых зависимостей.
- Обновить `АУДИТ-ПРОИЗВОДИТЕЛЬНОСТИ.md` или создать `docs/performance/video-quality-size-policy-2026-11-08.md`.

---

### Task 1: Снять исходные замеры и проверить готовый инструмент

**Files:**
- Modify: `АУДИТ-ПРОИЗВОДИТЕЛЬНОСТИ.md` or create `docs/performance/video-quality-size-policy-2026-11-08.md`

**Interfaces:**
- Inputs: VideoShrink 1.1.5, bundled FFmpeg/ffprobe, контрольный ролик предыдущего аудита.
- Produces: измеренная baseline-таблица и решение «поставлять ab-av1» либо «использовать совместимый native backend».

- [x] **Step 1: Подтвердить контрольный ролик без изменения исходника.** Проверить ожидаемые 62 843 587 байт, примерно 59,63 секунды, 1 789 кадров и 1080×1920. Искать в `dist/VideoShrink_Portable_1.1.1/Input` и `dist/VideoShrink_Portable_1.1.3/Input`. Если точного файла нет, отметить это и отделить исторические данные от новых замеров; не подменять ролик другим.

- [x] **Step 2: Снять baseline до правки кода.** На подтверждённом файле записать полное время, размер, декодируемость и равенство frame hashes для lossless preset fast/medium; также время, размер и полный VMAF действующей двухпроходной ABR-ветки при фиксированной цели. Зафиксировать Windows/CPU, FFmpeg, x265/libvmaf и параметры. Результаты старого отчёта fast 178,63 с / 856 371 943 байта и medium 338,26 с / 581 436 205 байт пометить как исторические, а не как повторный запуск.

- [x] **Step 3: Провести compatibility gate ab-av1.** Используя [официальный Windows-релиз ab-av1 v0.11.7](https://github.com/alexheretic/ab-av1/releases/tag/v0.11.7) и [README проекта](https://github.com/alexheretic/ab-av1/blob/main/README.md), запустить короткий CRF/VMAF search через JSON-вывод с путём именно к bundled FFmpeg и libx265. Проверить, что доступны CRF, sample VMAF, прогноз размера/времени и корректные stream mappings; зафиксировать версию/SHA/лицензию. Если запуск требует недоступный кодек либо теряет дорожки, выбрать native backend поверх существующих компонентов, не добавляя libsvtav1.

- [x] **Step 4: Записать baseline и решение по backend.** Не выводить универсальные оценки из одного ролика.

---

### Task 2: Исправить правила размера, strict-проверку и переход к fallback

**Files:**
- Modify: `videoshrink/core.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- `output_size_is_valid(mode: str, source_size: int, output_size: int, max_size_bytes: int = MAX_SIZE_BYTES) -> bool`.
- `VideoProcessor.frame_hashes(path: Path) -> tuple[str, ...]`.
- Сохранить `VideoProcessor.encode(..., mode=...)` как основной интерфейс обработки.

- [x] **Step 1: Добавить тесты правил размера.** `test_strict_lossless_accepts_smaller_than_source_above_two_gib` проверяет strict-результат больше 2 ГБ, но меньше источника; `test_adaptive_requires_strictly_below_limit_at_boundary` отклоняет размер ровно на лимите; `test_all_modes_reject_output_not_smaller_than_source` отклоняет равный/больший источника результат. Запуск: `python -m unittest discover -s tests -p test_core.py -k test_strict_lossless_accepts_smaller_than_source_above_two_gib -v`. Ожидается FAIL на текущем жёстком лимите strict.

- [x] **Step 2: Реализовать общий предикат результата.** Всегда требовать output меньше source. Только в adaptive, если источник достиг эффективного лимита `min(max_size_bytes, MAX_SIZE_BYTES)`, дополнительно требовать output строго меньше лимита. Использовать этот предикат в core и далее в history/публикации.

- [x] **Step 3: Добавить тесты хешей кадров.** `test_strict_rejects_mismatched_decoded_frame_hashes` проверяет отказ при одинаковом количестве, но различных хешах; `test_strict_accepts_matching_decoded_frame_hashes` проверяет принятие совпадающих последовательностей. Запустить оба через unittest discovery с `-k имя_теста`; до реализации ожидается FAIL.

- [x] **Step 4: Проверить кадры за один полный проход.** Реализовать `frame_hashes()` через FFmpeg `-xerror` и `framemd5` для source/output, сравнить последовательности и число декодированных кадров. Совместить декодирование результата и frame-hash проверку; не запускать отдельную повторную проверку всего видео. Несовпадение удаляет только partial.

- [x] **Step 5: Добавить тесты малого adaptive и повтора.** `test_adaptive_falls_back_for_source_below_two_gib` проверяет fallback на малом файле после неподходящего lossless-кандидата. `test_adaptive_does_not_repeat_lossless_during_size_correction` считает вызовы и требует ровно один lossless-проход даже при lossy-коррекциях. Ожидается FAIL на текущем раннем завершении.

- [x] **Step 6: Реализовать переходы.** Strict возвращает `NO_REDUCTION`, если lossless-кандидат не меньше источника, без ограничения в 2 ГБ. Adaptive принимает lossless только если он меньше источника и соблюдает лимит; иначе удаляет его и продолжает к поиску качества независимо от исходного размера. После этого lossless повторно не запускается.

- [x] **Step 7: Проверить core.** Запустить `python -m unittest discover -s tests -p test_core.py -v`; ожидается PASS для новых границ и существующих bitrate/retry тестов.

---

### Task 3: Добавить sample-based выбор CRF и ограниченную коррекцию

**Files:**
- Create: `videoshrink/quality_search.py`
- Modify: `videoshrink/core.py`
- Create: `tests/test_quality_search.py`
- Modify: `tests/test_core.py`

**Interfaces:**
- `ProgressCallback = Callable[[str, float], None]`.
- `QualityCandidate(crf: float, sample_vmaf: float, estimated_size_bytes: int, estimated_seconds: float)`.
- `QualitySearchResult(selected: QualityCandidate, candidates: tuple[QualityCandidate, ...], threshold_met: bool)`.
- `QualitySearch(ffmpeg: Path, ffprobe: Path, temp_dir: Path, ab_av1: Path | None = None)`.
- `QualitySearch.find_candidate(source: MediaInfo, target_size_bytes: int, quality_threshold: float, progress: ProgressCallback | None = None) -> QualitySearchResult`.
- `VideoProcessor.__init__(..., quality_search: QualitySearch | None = None)` позволяет подставить search fake в тестах; по умолчанию используется bundled реализация.

- [x] **Step 1: Написать падающие тесты search.** Проверить `test_search_returns_best_fitting_candidate_and_marks_quality_warning` и выбор минимального CRF среди кандидатов, проходящих и VMAF, и прогноз размера; выбор лучшего кандидата с `threshold_met=False`, если порог недостижим; отказ при пустой/ошибочной выборке; монотонный прогресс; единый формат результата для ab-av1 и native backend. Запуск: `python -m unittest discover -s tests -p test_quality_search.py -v`; ожидается FAIL, так как модуля пока нет.

- [x] **Step 2: Реализовать search backend.** Использовать результат Task 1. Native backend берёт равномерно распределённые короткие фрагменты, кодирует тестовые варианты с тем же x265 preset, измеряет VMAF и оценивает полный размер/время; ограничить число sample-попыток и учитывать короткие ролики. Передавать FFmpeg прогресс как отдельный этап. Удалять sample-файлы после успеха, отказа и отмены.

- [x] **Step 3: Написать тесты целевого бюджета.** `test_small_source_target_is_strictly_below_source_with_reserve`; `test_large_source_uses_1900000000_default_target`; `test_user_target_is_clamped_to_source_and_hard_limit`. Запускать по одному через unittest с `-k имя_теста`; до helper ожидается FAIL.

- [x] **Step 4: Реализовать target и выбор.** Для входа ниже лимита ограничить цель пользовательским значением и размером входа с текущим 5% запасом кодирования; для входа от лимита использовать default 1 900 000 000 байт. Выбирать наименьший CRF из помещающихся кандидатов; если VMAF-порог недостижим — выбрать наименьший CRF из помещающихся и вернуть предупреждающий флаг. Sample-прогноз никогда не заменяет проверку точных байтов.

- [x] **Step 5: Написать тесты полного encode.** `test_adaptive_encodes_selected_crf_with_x265`; `test_adaptive_retries_only_lossy_encode_when_candidate_violates_hard_size`; `test_adaptive_reports_quality_below_threshold_without_rejecting_fit`; `test_adaptive_fails_cleanly_when_no_candidate_can_fit`. Проверить, что повтор получает новый CRF, lossless не повторяется, а непригодный результат не публикуется.

- [x] **Step 6: Реализовать полный encode и коррекцию.** Кодировать выбранный CRF во временный файл x265, сохранить текущую проверку полного декодирования/дорожек, измерить реальные байты и полный VMAF. Если нарушен hard limit или результат не меньше источника, удалить partial и скорректировать CRF по измеренному отношению размера, используя предыдущие границы; разрешить только настроенные 0–3 lossy-повтора. Подходящий файл ниже VMAF-порога принять с явной оценкой/предупреждением.

- [x] **Step 7: Проверить потоки и очистку.** `test_unsupported_stream_fails_without_publishing_output` и `test_failed_candidate_is_deleted_and_source_is_unchanged` проверяют, что несовместимый subtitle/attachment/HDR или дополнительные video tracks дают понятную ошибку и не создают успешный выход. Маппить совместимые аудиодорожки, главы и метаданные; не использовать только `0:v:0`, если дополнительные потоки не проверены.

- [x] **Step 8: Запустить проверки.** `python -m unittest discover -s tests -p test_quality_search.py -v`, затем `python -m unittest discover -s tests -p test_core.py -v` и `python -m unittest discover -s tests -v`; ожидается PASS.

---

### Task 4: Синхронизировать настройки, очередь, историю и прогресс

**Files:**
- Modify: `videoshrink/app.py`
- Create: `tests/test_app_workflow.py`

**Interfaces:**
- `History.completed(key: str, output: Path, *, mode: str, max_size_bytes: int = MAX_SIZE_BYTES) -> bool` проверяет сохранённый размер источника и фактический output через `output_size_is_valid`.
- Добавить чистые функции нормализации путей, проверки режима и отображения этапа в общий прогресс, доступные для unit-тестов без Tk root.

- [x] **Step 1: Добавить тесты интерфейсного workflow.** Проверить default при отсутствующем/повреждённом config, сохранение режима, синхронизацию settings/main, текст предупреждения только при выборе, отказ history для результата не меньше source, strict history с результатом >2 ГБ, дедупликацию нормализованных путей, монотонный процент и ETA для search/encode/verify, `test_cancel_removes_only_current_partial`, `test_disk_space_failure_does_not_publish_partial` и `test_start_ignores_repeat_while_processing`. Запуск: `python -m unittest discover -s tests -p test_app_workflow.py -v`; ожидается FAIL до добавления helpers.

- [x] **Step 2: Синхронизировать селекторы режима.** Добавить radio selector в окно настроек; оба окна читают/записывают `compression_mode`. Оставить строгий режим по умолчанию. При выборе adaptive показывать предупреждение о возможной видимой потере качества; убрать блокирующее предупреждение из `_startup()`. Старт обработки — только явная кнопка.

- [x] **Step 3: Применить общий предикат к history и публикации.** Передавать mode/лимит во все cache checks и добавить версию алгоритма сжатия в processing signature, чтобы история не использовала результаты прежнего алгоритма. В конце всегда сверять выход с фактическим размером source; для adaptive при достижении эффективного лимита требовать строго меньший результат. При нарушении удалить текущий partial и не писать успешную строку истории.

- [x] **Step 4: Нормализовать очередь и очистку.** Убрать дубли по resolved/case-normalized Windows путям до запуска worker. Сохранить однопоточную обработку и текущую защиту повторного запуска. При ошибке/отмене удалять только текущие partial и sample-данные.

- [x] **Step 5: Добавить видимый этап поиска и ETA.** Добавить этап «Подбор качества по фрагментам». Сопоставить lossless, поиск, каждый полный lossy encode и финальную проверку с монотонным общим процентом; сохранить существующие проценты и пульсацию заполненной зелёной части. ETA учитывать время sample search и обновляться по callback и завершённым этапам. Расширить безопасный лог полями длительности этапа, режима, параметров encode/search, размера кандидата, кода возврата и причины отказа; не логировать содержимое видео.

- [x] **Step 6: Проверить workflow.** Запустить workflow-тесты и полный unittest suite. Запустить GUI из исходников, убедиться в idle при старте, выбрать оба режима в главном экране и настройках, проверить сохранение и убедиться, что обработка начинается только после кнопки.

---

### Task 5: Сравнить полную обработку и собрать portable 1.1.6

**Files:**
- Modify: `videoshrink/app.py`, `build.ps1`, `VideoShrink.spec`, `installer.nsi`, `installer.iss`, `README.md`
- Modify: `licenses/` only when a third-party binary/notice is actually included
- Update: `АУДИТ-ПРОИЗВОДИТЕЛЬНОСТИ.md` or `docs/performance/video-quality-size-policy-2026-11-08.md`

**Interfaces:**
- Input: тот же подтверждённый контрольный ролик Task 1.
- Outputs: повтор fast/medium, сравнение старой ABR и нового adaptive, отчёт, `dist/VideoShrink_Portable_1.1.6.zip` и portable `VideoShrink.exe`.

- [x] **Step 1: Повторить полный fast/medium benchmark.** На том же полном ролике измерить время, байты, полное декодирование и frame-hash equality; сохранить источник. Рассчитать разницу с baseline и отдельно со старыми историческими цифрами.

- [x] **Step 2: Сравнить adaptive с baseline ABR.** При одинаковой цели, аудио и входе измерить время поиска, число полных попыток, итоговые байты, полное среднее VMAF, нижний процентиль/сегменты и, если доступно, пик памяти/среднюю загрузку CPU. Выполнить минимум один full-clip прогон. Если дополнительных репрезентативных видео нет, прямо ограничить вывод контрольным роликом.

- [x] **Step 3: Выбрать release preset по результатам.** Выбрать fast/medium по фактическому времени/размеру при точном равенстве кадров; обновить `LOSSLESS_PRESET` и processing signature, чтобы история не принимала старые результаты. Sample/final lossy encode используют один и тот же выбранный preset.

- [x] **Step 4: Поднять версию до 1.1.6.** Синхронизировать версию приложения, установщика, сборки и README. Добавить ab-av1 binary/license только если он прошёл compatibility gate и действительно используется; иначе не менять набор FFmpeg.

- [x] **Step 5: Собрать и проверить портативный артефакт.** Выполнить `python -m unittest discover -s tests -v`, проверки компиляции/import из `build.ps1` и `.\build.ps1`. Проверить EXE, bundled FFmpeg/ffprobe, лицензии и папки Input/Output/Logs/Temp; проверить ZIP на полноту. Запустить упакованный EXE с чистой временной папкой данных, без внешнего Python/FFmpeg.

- [x] **Step 6: Записать сравнение до/после.** В отчёте указать время, размер, экономию, точность/качество, число попыток, FFmpeg, ОС/CPU и неохваченные случаи; различать новые замеры и исторические сведения.

- [x] **Step 7: Передать проверенные пути и фактические результаты.** Назвать абсолютные пути ZIP и EXE и измеренные изменения времени/размера/качества. Не заявлять успех сборки или теста до осмотра артефактов и вывода проверок.

---

## Verification and Review

После каждой задачи запускать её focused tests; перед сборкой выполнить `python -m unittest discover -s tests -v`. Benchmark запускается на полном исходном ролике, а не на укороченном sample. Перед передачей проверить acceptance criteria спецификации, результаты сравнения, состав ZIP и чистый запуск EXE.

В каталоге проекта отсутствуют Git-метаданные, поэтому план не содержит commit-steps. Базовые и итоговые замеры вместе с отчётом сохраняют проверяемое сравнение.


