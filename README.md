# LGO - Local Generation Object

LGO - локальный Windows-сервис для генерации 3D-объектов из одного изображения или из четырёх ракурсов на базе Hunyuan3D-2.1.

Приложение запускается локально, использует модели и исходники Hunyuan3D на вашем компьютере и даёт удобный web-интерфейс для работы с генерациями.

## Возможности

- режимы входа: одно изображение или четыре ракурса `front`, `back`, `left`, `right`;
- пресеты качества геометрии: Fast, Balanced, High;
- пресеты типа объекта: Organic и Hard surface;
- генерация без текстуры или с PBR-текстурой;
- отдельный выбор скорости текстуры: Fast, Balanced, High;
- повторный запуск текстурирования для уже созданной белой модели;
- постобработка: очистка фона, удаление нижней плоской подставки, сглаживание, weighted normals, базовая правка пальцев рук и ног;
- экспорт в GLB, OBJ и FBX;
- история генераций с возможностью загрузить результат обратно в окно просмотра;
- отдельный рейтинг 1-5 звёзд для белой модели и для текстурированного результата.

## Что не входит в репозиторий

В репозиторий намеренно не добавлены тяжёлые локальные данные:

- `.venv/` - виртуальное окружение Python;
- `.cache/` - кеши Hugging Face и Python;
- `vendor/` - исходники Hunyuan3D-2.1;
- `wheelhouse/` - локальные CUDA/PyTorch wheels;
- `runs/` - история генераций, входные картинки, логи и готовые модели;
- `logs/` - логи сервиса;
- веса моделей Hunyuan3D.

Эти файлы должны храниться локально и не должны попадать в GitHub.

## Требования

- Windows 10/11;
- Python 3.10;
- NVIDIA GPU с поддержкой CUDA;
- CUDA Toolkit, если нужно собирать native rasterizer для текстур;
- Blender;
- скачанные веса Hunyuan3D-2.1 с Hugging Face.

Основные пути настраиваются в:

```text
config/lgo_config.json
```

В конфиге можно использовать `{project_root}` для путей, которые должны зависеть от папки проекта.

## Установка

Склонировать проект:

```powershell
git clone https://github.com/NikitaZ23/LGO.git
cd LGO
```

Создать виртуальное окружение:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1
```

Если Python 3.10 не находится автоматически, передайте путь явно:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1 -Python C:\Path\To\Python310\python.exe
```

Скачать исходники Hunyuan3D-2.1 в папку `vendor`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_hunyuan_source.ps1
```

Установить AI runtime:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_ai_runtime.ps1
```

## Модели

Скачайте нужные папки моделей Hunyuan3D-2.1 и поправьте `config/lgo_config.json`, если используете другие пути.

Пути по умолчанию:

```text
E:\AI\Models\Hunyuan3D-DiT-v2-1
E:\AI\Models\Hunyuan3D-DiT-v2-mv
E:\AI\Models\Hunyuan3D-Paint-v2-1\hunyuan3d-paintpbr-v2-1
E:\AI\Models\Hunyuan3D-Paint-v2-1\hunyuan3d-vae-v2-1
E:\AI\Models\Hunyuan3D-Paint-v2-1\hy3dpaint
E:\AI\Models\Hunyuan3D-Paint-v2-1\hy3dpaint\ckpt\RealESRGAN_x4plus.pth
```

Для геометрии нужны:

- `Hunyuan3D-DiT-v2-1` - генерация из одного изображения;
- `Hunyuan3D-DiT-v2-mv` - генерация из четырёх ракурсов.

Для PBR-текстур нужны:

- `hunyuan3d-paintpbr-v2-1`;
- `hunyuan3d-vae-v2-1`;
- `hy3dpaint`;
- `RealESRGAN_x4plus.pth`.

## Запуск

Запуск в обычном окне:

```powershell
.\start-lgo.bat
```

Запуск в фоне:

```powershell
.\start-lgo-background.bat
```

Остановка:

```powershell
.\stop-lgo.bat
```

После запуска открыть:

```text
http://127.0.0.1:7865
```

В верхней панели фронта есть кнопки:

- `Refresh` - обновить статус окружения;
- `Restart` - перезапустить локальный сервис LGO;
- `Shutdown` - выключить локальный сервис LGO.

Если сервис был запущен через `start-lgo.bat`, при штатном `Restart` или `Shutdown` старое окно `cmd` закроется само.
Окно останется с паузой только если сервер завершился с ошибкой, чтобы можно было прочитать сообщение.

После `Shutdown` включить сервис обратно можно через `start-lgo-background.bat` или `start-lgo.bat`.
Лог автоперезапуска пишется в `logs/service-restart.log`.

## Проверка окружения

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_environment.ps1
```

Проверка покажет Python, venv, Blender, GPU, модели, зависимости и готовность PBR runtime.

## Работа с результатами

Каждая генерация сохраняется в папке:

```text
runs/<job-id>/
```

Внутри job обычно есть:

- `input/` - исходные изображения;
- `input/cleaned/` - очищенные изображения после preprocessing;
- `output/white_mesh.glb` - белая модель без текстуры;
- `output/textured_mesh.glb` или `output/textured_mesh_stable.glb` - текстурированная модель;
- `job.json` - состояние задачи, настройки, outputs, предупреждения и рейтинги;
- `run.log` - лог выполнения.

История на фронте строится из папок `runs`.
При загрузке из истории фронт сначала открывает `white_mesh.glb`, если он есть.
Текстурированный `textured_mesh.glb` остается доступен через переключатель `Textured mesh` в окне результата.
Чекбокс `Показать с текстурой` в заголовке результата быстро переключает просмотр между белой моделью и текстурированной версией.

## Примечания

Текстурирование обычно заметно медленнее генерации геометрии. Практичный порядок работы:

1. Сначала сгенерировать белую модель без текстуры.
2. Оценить форму в viewer.
3. Если форма хорошая, нажать `Add texture`.
4. Начинать с Fast texture, а Balanced или High использовать только для удачных моделей.

Тип объекта влияет на постобработку. Для персонажей и ткани используйте `Organic`.
Для механики, оружия, брони и четких промышленных форм используйте `Hard surface`.
Для камней, минералов и неровных природных объектов используйте `Rock / stone`: этот режим делает текстуру матовее, приглушает лишний PBR-блеск и мягче сглаживает видимые грани.

Если на модели сильно видны полигоны, новые GLB проходят через Blender post-process со сглаживанием и weighted normals. Это улучшает отображение в viewer, но не заменяет полноценную высокополигональную реконструкцию.

Большие модели, веса, локальные генерации и временные файлы не коммитьте в GitHub.
