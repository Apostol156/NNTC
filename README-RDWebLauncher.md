# RD Web Launcher (RDWebLauncher.exe)

Лаунчер публикаций RD Web Access: при запуске получает текущего пользователя
Windows (**без запроса пароля**), подключается к RD Web с Windows-аутентификацией
(Negotiate/Kerberos через текущий логон-токен SSPI), загружает список ресурсов,
строит дерево папок, а по двойному клику скачивает .rdp (или формирует его по
согласованным с администратором правилам из `config.ini`) и запускает стандартным
клиентом `mstsc.exe`.

## Файлы проекта

| Файл | Назначение |
|---|---|
| `rdweb_launcher.py` | Исходный код лаунчера (GUI на tkinter + вся логика) |
| `requirements.txt` | Зависимости: `requests`, `pyspnego` (Kerberos/Negotiate) |
| `config.ini` | Создаётся автоматически рядом с exe: URL RD Web, правила .rdp, путь mstsc |
| `tests/test_rdweb_launcher.py` | Юнит-тесты парсинга/генерации (7 шт.) |
| `.github/workflows/build.yml` | CI: сборка exe на windows-latest, артефакт `RDWebLauncher.exe` |

## Сборка .exe (на Windows)

```bat
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name RDWebLauncher rdweb_launcher.py
:: результат: dist\RDWebLauncher.exe
```

Либо готовый exe приходит как **артефакт сборки** GitHub Actions
(вкладка Actions → Build RDWebLauncher.exe → artifact `RDWebLauncher-exe`).

## Настройка (config.ini)

```ini
[rdweb]
url = https://rdweb.contoso.com/RDWeb/Pages/en-US/Default.aspx
verify_ssl = true

[rdp]
; правила формирования .rdp, согласованные с администратором:
screenmodeid:i = 1
session bpp:i = 32

[launcher]
mstsc = C:\Windows\System32\mstsc.exe
```

## Требования к инфраструктуре

* Рабочая станция в домене Active Directory, пользователь залогинен в домене.
* На RD Web включена Windows-аутентификация (Negotiate/Kerberos), для NTLM-fallback —
  добавьте хост в Local Intranet Zone в IE policy.
* Kerberos-SPN `http/<host>` зарегистрирован за сервером RD Web (обычно делает
  установка RDS).

## Тесты

```bash
python -m pytest tests -q
```
