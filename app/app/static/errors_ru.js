/* Единая карта «английский detail API → русский текст» (пункт плана 3.14).
   Контракт API остаётся английским (на нём тесты); пользователю показываем русский. */
(function () {
  const MAP = {
    'Forbidden': 'Недостаточно прав для этого действия.',
    'Not authenticated': 'Нужно войти в систему.',
    'Invalid token': 'Сессия недействительна. Войдите заново.',
    'Session expired': 'Сессия завершена: личный пароль изменён, сброшен или обязателен. Войдите заново.',
    'User not found': 'Пользователь не найден. Войдите заново.',
    'Too many login attempts': 'Слишком много неудачных попыток входа. Подождите несколько минут и попробуйте снова.',
    'Invalid credentials': 'Неверный пароль.',
    'Login is not on the allowlist': 'Этот логин не в списке допущенных.',
    'Name and Telegram ID required': 'Укажите имя и Telegram ID.',
    'Not found': 'Не найдено.',
    'Project name required': 'Введите название проекта.',
    'Project not found': 'Проект не найден.',
    'Artifact not found': 'Файл не найден.',
    'Entity not found': 'Запись не найдена.',
    'Upload not found': 'Загрузка не найдена.',
    'Member not found': 'Участник не найден.',
    'Report not found': 'Отчёт не найден.',
    'Audio file not found': 'Аудиофайл не найден.',
    'Meeting not found': 'Встреча не найдена.',
    'Meeting is not live': 'Встреча сейчас не идёт: она ещё не началась или уже завершена.',
    'Meeting has not started': 'Встреча ещё не началась.',
    'Meeting ended': 'Встреча уже завершена.',
    'Meeting is not ended': 'Встреча ещё не завершена.',
    'Participant not found': 'Участник не найден в комнате.',
    'display_name required': 'Введите имя.',
    'guest_id invalid': 'Не удалось определить устройство. Обновите страницу.',
    'No speaker recordings': 'Нет записей участников для обработки.',
    'No files': 'Файлы не выбраны.',
    'Transcript not available': 'Расшифровка ещё не готова.',
    'Summary not available': 'Резюме ещё не готово.',
    'Owner already has access': 'У владельца проекта уже есть доступ.',
    'Role must be manager or user': 'Роль должна быть «менеджер» или «пользователь».',
    'already resolved': 'Уже решено.',
    'Invalid JSON body': 'Некорректный запрос.',
    'Analyze already running': 'Обработка созвона уже идёт.',
    'Audio WAV not found for artifact': 'Аудиофайл не найден.',
    'Audio artifact not found': 'Аудиофайл не найден.',
    'Audio too long': 'Аудио слишком длинное.',
    'Brief summary not available': 'Краткое резюме ещё не готово.',
    'Brief summary not found': 'Краткое резюме не найдено.',
    'Cannot change own access': 'Нельзя менять собственный доступ.',
    'Cannot remove own access': 'Нельзя убрать собственный доступ.',
    'Conflict not found': 'Конфликт не найден.',
    'Download not available yet': 'Скачивание пока недоступно.',
    'End meeting before deleting it': 'Сначала завершите встречу, затем удаляйте.',
    'File too large': 'Файл слишком большой.',
    'Frame not found': 'Кадр не найден.',
    'Frames are for audio artifacts only': 'Кадры есть только у записей созвонов.',
    'Internal Server Error': 'Ошибка на сервере. Попробуйте ещё раз.',
  };
  const BY_STATUS = {
    401: 'Нужно войти в систему.',
    403: 'Недостаточно прав для этого действия.',
    404: 'Не найдено.',
    409: 'Действие сейчас недоступно.',
    413: 'Файл слишком большой.',
    422: 'Проверьте введённые данные.',
  };
  const LATIN_ONLY = /^[\x00-\x7F]+$/;

  function text(detail, status) {
    const raw = typeof detail === 'string' ? detail.trim() : '';
    if (raw && MAP[raw]) return MAP[raw];
    if (raw && !LATIN_ONLY.test(raw)) return raw; // уже по-русски
    if (status >= 500) return 'Ошибка на сервере. Попробуйте ещё раз.';
    if (BY_STATUS[status]) return BY_STATUS[status];
    return raw ? 'Не удалось выполнить действие (' + raw + ').' : 'Не удалось выполнить действие.';
  }

  window.SKKKKErrors = { text: text, MAP: MAP };
})();
