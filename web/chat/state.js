// フェーズ6 S4（リファクタリング計画）: 複数モジュールをまたいで参照される共有可変状態＋定数の集約。
// 葉モジュール＝何も import しない（window.Sherpa 等のグローバル参照が必要なモジュールは各々で参照する）。
'use strict';

// 共有可変状態の集約（フェーズ6 S3・地雷3/5対応）: モジュール境界を跨いで読み書きされる状態を
// const S = {...} の1個へ集約し、以後は S.xxx で参照する（プロパティ書換は import 後も安全＝
// import した let への代入 TypeError を回避）。単一ドメイン内で閉じる状態（例: brain-menu の
// _agent/_models・share-dialog の _inviteeChips 等）は各セクションの局所 let のまま残す
// （判断基準・後続スライスでそのままモジュール内 let にしてよい）。
export const S = {
  cid: null, es: null, nodes: {},
  liveTurnId: null,
  turnStartedAtMs: 0,   // 実行中ターンの開始時刻（ms）。send が今を、resumeRunningTurn がサーバの started_at を入れる＝画面遷移後も経過表示が通算になる, turnSeq: 0,   // 積み上げ表示（右ペイン）: 現在ライブ中のターン要素id・連番
  // 背景実行（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md）: サーバが払い出す turn_id。
  // EventSource は GET /chat/turns/{turnId}/stream を cursor=0 から購読する（切断/再購読は購読解除に
  // すぎない＝ターン自体はサーバ側で継続。停止は POST /chat/turns/{turnId}/stop で明示的に行う）。
  turnId: null,
  scope: [], scopeLabels: {}, scopeTree: null, currentScopeMeta: null,   // 明示選択/見出し/ツリー/直近の使用範囲（D）
  lens: 'auto', layer: 'both',   // 調べ方ブロック（SC-6b）: 調べ方の明示選択（既定=自動）・探す対象（既定=両方）
  depthProfile: 'standard',   // 調べる深さ（調べ方ブロック §3.2・SC-6c）: 既定=標準（新規会話は常に標準）
  tools: { grep: true, fulltext: true, graph: true },   // 検索経路トグル（調べ方ブロック §3.6・SC-6e）: 既定=全ON
  // 軸ごとの「ユーザー操作由来の明示 ON/OFF」フラグ（SC-6e）: チップ操作・「OFFにした検索を戻す」
  // で該当軸を true にする。会話ロード・後追い復元・新規会話リセットでは false（未操作）へ戻す。
  // 未操作＝既定値のままの軸だけを送信 body から省略できる（inquiry.js::toolsForSend 参照）——
  // 全軸を一律「値だけ」で見ると、明示的に戻した/変更した軸まで既定値と区別できず省略してしまう。
  toolsExplicit: { grep: false, fulltext: false, graph: false },
  verLabels: {},   // 取込ディレクトリ識別子→表示名（例 v1→4期・/world-options 由来）
  pendingConvWorld: null,   // 会話復元が選択肢の読込より先に走った時の後追い適用（deep-link ?conv= の競合対策）
  kb: false,               // ナレッジ参照（既定オフ）
  kbLocked: false,         // Codex 構成は資料参照ON固定（決定 2026-08-15・サーバ側でも強制）
  personal: false,         // Feature B: 個人ファイル参照トグル（既定オフ）
  webSearch: false,        // WEB-1: Codex の Web 検索をこのチャットで希望するか（既定オフ・新規会話は常にオフ）
  convHasPersonal: false,  // Feature C: 現在の会話が個人コンテンツを参照済みか
  ansEl: null, ansHead: null,   // 逐次表示中の回答カード本体/見出し（reveal 用・元は描画セクション内で宣言）
  liveTraceTree: null,   // EXT-4: trace_version=2 のときだけ張る階層描画ツリー（TraceTreeV2・v1 は null のまま）
  sending: false,   // send() の開始POST応答待ち中（true の間、send() 先頭で再入を拒否する）
};

// 質問例（クリックで入力欄に流し込む・自動送信しない）。どれもそのまま送信して意味が通る具体的な
// 質問文で統一する（編集前提の未完成文・会話の続き前提の文は置かない）。4レンズ（影響/トラブル/
// 仕様問い合わせ/資料作成）を一通り示す。
// welcome() の例示ボタン描画と、クリックで入力欄へ流し込む処理の双方（別モジュール）から参照される
// ＝実依存を grep で確認のうえ複数モジュール共有の定数として集約（S4）。
export const EXAMPLES = [
  '消費税率を変更すると、影響がありそうな箇所を教えてください。',
  '夜間バッチが異常終了しました。原因の候補を教えてください。',
  '消費税の端数処理の仕様を教えてください。',
  '登録されている資料の内容を要約した概要資料を作ってください。',
];
