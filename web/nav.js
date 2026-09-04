// 全ページ共通の上部ナビ（タブ）＝再利用コンポーネント。各ページは <sherpa-topbar></sherpa-topbar>
// を置き、このファイルを <head> で読み込むだけ。現在の画面は URL から判定してタブを強調する。
// テーマ切替ボタン(#themebtn)は要素だけ用意し、挙動は各ページのスクリプトが担う（ページ固有の
// 再描画があるため）。リンク/ラベルはすべて固定値＝XSS 非該当。
'use strict';

(function () {
  const LINKS = [
    ['home.html', 'ホーム'],
    ['chat.html', 'チャット'],
    ['manual.html', '使い方'],
    ['settings.html', '個人設定'],
  ];
  // ログイン済みユーザー全員に表示するリンク（互換モードも含む）。
  const USER_LINKS = [
    ['workspace.html', 'マイワークスペース'],
  ];
  // admin 専用リンク（役割が admin のときのみ表示）。S1（2026-07-08-設定分離とUI整備.md）:
  // 個別の管理系リンク（ユーザー管理/監査/利用統計/状態）は「システム管理」（admin-settings.html）に
  // 入口を集約した。admin-settings.html 内の管理メニューから各画面へ遷移する（右上ナビは2項に整理）。
  // 「資料」（W1・2026-09-03）: 資料フォルダの登録・更新・削除・グラフ生成に加え、下段の取り込み
  // 状況（`/ingest/preview`）・全文検索（`/admin/es/search`）も含め、この画面の全データ取得が
  // 既に admin 限定（`_require_admin`）——一般ユーザーには開いても何も表示できない。
  // 「ナレッジグラフ」（CLEAN-1・2026-09-03・W1 の残・同型是正）: `/graph`・`/graph/facets`・
  // `/graph/search`・`POST /graph/ask` は既に admin 限定（`_require_admin`）だが、画面は一般
  // ユーザーへナビ表示され、開いても何も出ない不整合があった。ingest.html（W1）と同じ手当て。
  const ADMIN_LINKS = [
    ['ingest.html', '資料'],
    ['graph.html', 'ナレッジグラフ'],
    ['admin-settings.html', 'システム管理'],
  ];

  // バックエンド健全性の状態ドット用ポーリング間隔（ms）。非表示タブは Sherpa.visibilityInterval
  // が自動で止める（性能台帳 QW4）ので、可視タブの定常負荷を下げるためここは長めにしてよい。
  const HEALTH_POLL_MS = 45000;
  // 背景実行チャットターン（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md）の「実行中」表示の
  // ポーリング間隔（ms）。非表示タブは止まる前提（同上）で 45〜60秒でも十分（proposal §4）。
  const TURNS_POLL_MS = 45000;
  // connectedCallback が複数回走っても visibilityInterval（内部の setInterval・
  // visibilitychange リスナー）が重複しないよう、モジュールスコープで保持。
  let _healthTimer = null;
  let _turnsTimer = null;
  // admin かどうか（/auth/me 成功時のみ判明）。ポーリング開始自体は認証結果と独立させるため、
  // ここに保持して applyHealth から参照する（クリックで詳細リンク・title の出し分け用）。
  let _isAdminUser = false;

  class SherpaTopbar extends HTMLElement {
    connectedCallback() {
      const here = location.pathname.split('/').pop() || 'chat.html';
      this.className = 'topbar';
      this.innerHTML =
        '<div class="brand"><span class="mark">⛰</span>Sherpa <small>業務AI基盤</small></div>'
        + '<nav class="nav" id="sherpa-nav">'
        + LINKS.map(([href, label]) => {
            const on = href === here ? ' class="on"' : '';
            return `<a${on} href="${href}">${label}</a>`;
          }).join('')
        + '</nav>'
        + '<div class="spacer"></div>'
        + '<a class="turnnotice" id="turnnotice" hidden></a>'
        + '<a class="healthdot" id="healthdot" title="サービス状態を確認中…" hidden></a>'
        + '<button class="iconbtn" id="themebtn" title="テーマ切替">🌙</button>'
        + '<div class="userwrap" id="userwrap">'
        + '<button class="user" id="topbar-user" title="現在のユーザー" aria-haspopup="true" aria-expanded="false">…</button>'
        + '<div class="usermenu" id="usermenu" hidden role="menu">'
        + '<div class="um-h"><b id="um-name">…</b><small id="um-role"></small></div>'
        + '<a class="umitem" id="um-changepw" href="change-password.html" role="menuitem">パスワード変更</a>'
        + '<button class="umitem" id="um-logout" type="button" role="menuitem">ログアウト</button>'
        + '<div class="um-note" id="um-note" hidden>認証は無効です（開発モード）</div>'
        + '</div></div>';

      // ユーザードロップダウン（全ページ共通・UIフィードバック 2026-07-03）。
      // 開閉は #topbar-user クリックでトグル、外側クリック／Escape で閉じる（.brainmenu と同じ流儀＋a11y追加）。
      const userBtn = document.getElementById('topbar-user');
      const userMenu = document.getElementById('usermenu');
      const closeUserMenu = () => {
        const m = document.getElementById('usermenu');
        if (m) m.hidden = true;
        const b = document.getElementById('topbar-user');
        if (b) b.setAttribute('aria-expanded', 'false');
      };
      if (userBtn && userMenu) {
        userBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const willOpen = userMenu.hidden;
          userMenu.hidden = !willOpen;
          userBtn.setAttribute('aria-expanded', String(willOpen));
        });
        userMenu.addEventListener('click', (e) => e.stopPropagation());   // メニュー内クリックで外側判定を発火させない
      }
      // RV再検証 LOW: document への委譲リスナーは要素（#userwrap 等）の生死と無関係に残るため、
      // connectedCallback が複数回走っても重複登録しないよう、ハンドラをインスタンスに保持して
      // 使い回す（初回だけ生成・以降は remove→add で必ず1本にする）。ハンドラ本体は呼び出し時に
      // 都度 document.getElementById する（closure で古い DOM 参照を握らない＝再構築後も正しく動く）。
      if (!this._onDocClick) {
        this._onDocClick = (e) => { if (!e.target.closest('#userwrap')) closeUserMenu(); };
      }
      if (!this._onDocKeydown) {
        this._onDocKeydown = (e) => {
          const m = document.getElementById('usermenu');
          if (e.key === 'Escape' && m && !m.hidden) {
            closeUserMenu();
            const b = document.getElementById('topbar-user');
            if (b) b.focus();
          }
        };
      }
      document.removeEventListener('click', this._onDocClick);
      document.removeEventListener('keydown', this._onDocKeydown);
      document.addEventListener('click', this._onDocClick);
      document.addEventListener('keydown', this._onDocKeydown);
      const logoutBtn = document.getElementById('um-logout');
      if (logoutBtn) logoutBtn.addEventListener('click', async () => {
        if (!confirm('ログアウトしますか？')) return;
        try { await fetch('/auth/logout', { method: 'POST' }); } catch (_) { /* best-effort */ }
        location.href = '/ui/login.html';
      });
      const changePwLink = document.getElementById('um-changepw');
      if (changePwLink) {
        try {
          changePwLink.href = 'change-password.html?next=' + encodeURIComponent(location.pathname + location.search);
        } catch (_) { /* 既定の change-password.html のまま */ }
      }

      // バックエンド健全性の状態ドット（即時＋定期ポーリング）。/auth/me の成否とは独立に開始する
      // （Postgres 停止時は /auth/me 自体が失敗しうるため、それに依存すると一番赤にしたい場面で
      // ドットが出なくなる）。401（未ログイン）だけソフトに無視（ドットは hidden のまま＝
      // ログイン画面では出さない）。それ以外の非 2xx・fetch 失敗は down として表示する。
      const dot = document.getElementById('healthdot');
      let pollHealth = null;
      if (dot) {
        const applyHealth = (h) => {
          if (!h || !h.status) return;
          dot.hidden = false;
          dot.classList.remove('ok', 'degraded', 'down');
          dot.classList.add(h.status);
          let title = h.status === 'ok'
            ? 'サービスは正常に動作しています'
            : h.status === 'degraded'
              ? '一部の機能が制限されています（検索・影響分析など）'
              : 'サービスに問題が発生しています。管理者にご連絡ください';
          if (_isAdminUser) {
            dot.href = 'status.html';
            title += '（クリックで詳細）';
          } else {
            dot.removeAttribute('href');
          }
          dot.title = title;
        };
        pollHealth = () => {
          fetch('/health/summary')
            .then((r) => {
              if (r.status === 401) return null;   // 未ログイン（従来どおり無視）
              if (r.ok) return r.json();
              return { status: 'down' };            // 非 2xx（認証DB到達不可等）
            })
            .then(applyHealth)
            .catch(() => applyHealth({ status: 'down' }));   // fetch 失敗（バックエンド到達不可）
        };
        pollHealth();
        if (_healthTimer) _healthTimer.stop();
        _healthTimer = Sherpa.visibilityInterval(pollHealth, HEALTH_POLL_MS);
      }

      // 背景実行チャットターン（覗き窓方式）の「実行中」表示（即時＋定期ポーリング・全ページ共通）。
      // GET /chat/turns/running はログイン必須のため、未ログイン（401）・fetch 失敗はどちらも
      // 「実行中なし」としてソフトに無視する（healthdot と異なりエラー状態を可視化する必要はない＝
      // 単に「今は無い」でよい）。クリックで該当会話（先頭のターン）を開く。
      const notice = document.getElementById('turnnotice');
      if (notice) {
        const applyTurns = (d) => {
          const turns = (d && d.turns) || [];
          if (!turns.length) { notice.hidden = true; return; }
          notice.hidden = false;
          notice.textContent = turns.length > 1 ? `⏳ 回答作成中（${turns.length}件）` : '⏳ 回答作成中';
          notice.href = `chat.html?conv=${encodeURIComponent(turns[0].conversation_id)}`;
          notice.title = 'クリックで該当の会話を開く';
        };
        const pollTurns = () => {
          fetch('/chat/turns/running')
            .then((r) => (r.ok ? r.json() : null))
            .then(applyTurns)
            .catch(() => applyTurns(null));
        };
        pollTurns();
        if (_turnsTimer) _turnsTimer.stop();
        _turnsTimer = Sherpa.visibilityInterval(pollTurns, TURNS_POLL_MS);
      }

      // /auth/me で現在ユーザーを取得し、ユーザーアバター・各種リンクを更新。
      // 互換モード（auth 無効）では admin 合成が返るので admin リンクは表示する。
      // 401 時（auth 有効かつ未ログイン）: ログイン画面へリダイレクト（ループ防止のため login.html は除外）。
      // ネットワーク障害・500 等: ソフトに無視（リダイレクトしない）。
      fetch('/auth/me').then((r) => {
        if (r.status === 401) {
          // ログイン画面自体はリダイレクトしない（無限ループ防止）。
          const currentPage = location.pathname.split('/').pop() || '';
          if (currentPage === 'login.html') return Promise.resolve(null);
          // next= を安全に構築: 同一オリジン・/ui/ パスのみ許可。
          let next = '';
          try {
            const url = new URL(location.href);
            if (url.origin === location.origin && url.pathname.startsWith('/ui/')) {
              next = '?next=' + encodeURIComponent(url.pathname + url.search + url.hash);
            }
          } catch (_) { /* 構築失敗時は next なしでログイン画面へ */ }
          location.href = '/ui/login.html' + next;
          return Promise.resolve(null);
        }
        return r.ok ? r.json() : Promise.resolve(null);
      }).then((u) => {
        if (!u) return;
        if (u.must_change_password) {
          const currentPage = location.pathname.split('/').pop() || '';
          if (currentPage !== 'change-password.html') {
            let next = '/ui/chat.html';
            try {
              const url = new URL(location.href);
              if (url.origin === location.origin && url.pathname.startsWith('/ui/')) {
                next = url.pathname + url.search + url.hash;
              }
            } catch (_) { /* 既定の chat に戻す */ }
            location.href = '/ui/change-password.html?next=' + encodeURIComponent(next);
            return;
          }
        }
        const av = document.getElementById('topbar-user');
        if (av) {
          const label = (u.display_name || u.uid || '?').slice(0, 1).toUpperCase();
          av.textContent = label;
          av.title = u.display_name || u.uid;
        }
        // ドロップダウンの中身（氏名/ロール）＋互換モード（認証OFF）での出し分け。
        const umName = document.getElementById('um-name');
        const umRole = document.getElementById('um-role');
        if (umName) umName.textContent = u.display_name || u.uid || '?';
        if (umRole) umRole.textContent = u.role === 'admin' ? '管理者' : 'ユーザー';
        const umChangePw = document.getElementById('um-changepw');
        const umLogout = document.getElementById('um-logout');
        const umNote = document.getElementById('um-note');
        const authOff = !!u.auth_disabled;
        if (umChangePw) umChangePw.hidden = authOff;
        if (umLogout) umLogout.hidden = authOff;
        if (umNote) umNote.hidden = !authOff;
        const nav = document.getElementById('sherpa-nav');
        // ログイン済み全員にマイワークスペースリンクを追加。
        if (nav) {
          USER_LINKS.forEach(([href, label]) => {
            const a = document.createElement('a');
            if (href === here) a.className = 'on';
            a.href = href;
            a.textContent = label;
            nav.appendChild(a);
          });
        }
        // admin ロールなら管理リンクをナビに追加。
        if (u.role === 'admin') {
          if (nav) {
            ADMIN_LINKS.forEach(([href, label]) => {
              const a = document.createElement('a');
              if (href === here) a.className = 'on';
              a.href = href;
              a.textContent = label;
              nav.appendChild(a);
            });
          }
        }
        // 健全性ドットの「クリックで詳細」表示を役割判明直後に反映（非 admin は false へ
        // 戻し、再接続・ロール変更で admin 表示が残留しないようにする）。
        _isAdminUser = (u.role === 'admin');
        if (pollHealth) pollHealth();
      }).catch(() => { /* auth 無効・ネットワーク障害でも問題なし（ドットのポーリングは継続） */ });
    }
    // RV再検証 LOW: 要素が DOM から外れたら document への委譲リスナーも解除する（残留防止）。
    disconnectedCallback() {
      if (this._onDocClick) document.removeEventListener('click', this._onDocClick);
      if (this._onDocKeydown) document.removeEventListener('keydown', this._onDocKeydown);
    }
  }
  if (!customElements.get('sherpa-topbar')) customElements.define('sherpa-topbar', SherpaTopbar);
})();
