// login.jsx — вход в настольную админку ПОД СВОИМ именем (app/desklogin.py).
//
// До этого вход был один на машину: токен из .env лежал в localStorage, и кто бы ни сел за
// компьютер студии, он работал правами первого владельца. Роли из-за этого в деске ничего не
// значили: наёмный оператор видел выручку, цены и кнопку удаления заказа.
//
// Пароля здесь нет намеренно — личность берётся из Telegram, где сотрудник уже заведён
// владельцем: он пишет боту /desk, получает шестизначный код и вводит его здесь.
//
// Свой fetch, а не общий слой api.js: тот возит авторизацию в каждом запросе, а вход — это
// как раз запрос БЕЗ авторизации, единственный в проекте. Тащить исключение в общий файл,
// которым пользуется ещё и клиентская мини-аппа, значило бы усложнить его ради одной двери.

const { useState, useEffect, useRef } = React;

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  return r.json().catch(() => ({ ok: false, error: 'Сервер ответил неразборчиво' }));
}

/** Кто мы для сервера и как устроен вход. null — сервер не ответил (бот не поднят). */
export async function deskWhoami() {
  try {
    let tok = '';
    try { tok = localStorage.getItem('pcb_admin_token') || ''; } catch (e) { tok = ''; }
    const r = await fetch('/api/desk/whoami?initData=' + encodeURIComponent(tok));
    return await r.json();
  } catch (e) {
    return null;
  }
}

export async function deskLogout() {
  let tok = '';
  try { tok = localStorage.getItem('pcb_admin_token') || ''; } catch (e) { tok = ''; }
  await post('/api/desk/logout', { initData: tok });
  try { localStorage.removeItem('pcb_admin_token'); } catch (e) { /* приватный режим */ }
  location.reload();
}

export function LoginScreen({ onDone }) {
  const [code, setCode] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const ref = useRef();

  useEffect(() => { if (ref.current) ref.current.focus(); }, []);

  const submit = async (e) => {
    if (e) e.preventDefault();
    const c = code.replace(/\D/g, '').slice(0, 6);
    if (c.length !== 6) { setErr('Код — шесть цифр'); return; }
    setBusy(true);
    setErr('');
    const r = await post('/api/desk/login', { code: c, device: navigator.userAgent });
    setBusy(false);
    if (!r || !r.ok) { setErr((r && r.error) || 'Код не подошёл'); setCode(''); return; }
    try { localStorage.setItem('pcb_admin_token', r.token); } catch (e2) { /* приватный режим */ }
    // Перезагрузка, а не переход состоянием: токен читает api.js при загрузке страницы, и
    // до перезагрузки весь слой данных остался бы в режиме «нет авторизации».
    if (onDone) onDone();
    location.reload();
  };

  return <div className="viewport">
    <div className="login">
      <div className="card login-card">
        <div className="lab" style={{ marginBottom: 14 }}>Вход в админку</div>
        <p className="login-hint">
          Напишите боту команду <b>/desk</b> — он пришлёт код на пять минут. Введите его здесь.
        </p>
        <form onSubmit={submit}>
          <input ref={ref} className="inp mono login-code" inputMode="numeric" autoComplete="off"
            placeholder="000000" value={code} maxLength={6}
            onChange={(ev) => { setCode(ev.target.value.replace(/\D/g, '')); setErr(''); }} />
          {err ? <div className="login-err">{err}</div> : null}
          <button className="db" type="submit" disabled={busy || code.length !== 6}
            style={{ width: '100%', justifyContent: 'center', marginTop: 12 }}>
            {busy ? 'Проверяю…' : 'Войти'}
          </button>
        </form>
        <p className="login-foot">
          Код действует только на этом компьютере: обменять его на вход можно лишь отсюда.
        </p>
      </div>
    </div>
  </div>;
}
