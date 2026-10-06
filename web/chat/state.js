// チャット画面の共有可変状態（S）と質問例の定数。何も import しない葉モジュール。
'use strict';

// モジュールをまたいで読み書きする状態は S.xxx で参照する（単一モジュール内で閉じる状態は各モジュールの局所 let）。
export const S = {
  cid: null, es: null, nodes: {},
  liveTurnId: null,
  turnStartedAtMs: 0,   // 実行中ターンの開始時刻（ms）
  // サーバが払い出す turn_id。GET /chat/turns/{turnId}/stream を購読する（切断してもターンは継続・停止は POST .../stop）。
  turnId: null,
  scope: [], scopeLabels: {}, scopeTree: null, currentScopeMeta: null,   // 明示選択/見出し/ツリー/直近の使用範囲
  lens: 'auto', layer: 'both',   // 調べ方（既定=自動）・探す対象（既定=両方）
  depthProfile: 'standard',   // 調べる深さ（既定=標準）
  tools: { grep: true, fulltext: true, graph: true },   // 検索経路トグル（既定=全ON）
  // 軸ごとの「利用者が明示操作したか」フラグ。未操作の軸だけ送信 body から省略する（inquiry.js::toolsForSend）。
  toolsExplicit: { grep: false, fulltext: false, graph: false },
  verLabels: {},   // 取込ディレクトリ識別子→表示名（/world-options 由来）
  pendingConvWorld: null,   // 会話復元が選択肢の読込より先に走った時の後追い適用
  kb: true,                // ナレッジ参照（既定ON）
  // 資料フォルダが1つも登録されていないと確定した（`GET /world-options` が空）ときだけ true。false は「未確認」を含む。
  kbForcedOff: false,
  personal: false,         // 個人ファイル参照トグル（既定オフ）
  webSearch: false,        // Codex の Web 検索を希望するか（既定オフ）
  convHasPersonal: false,  // 現在の会話が個人コンテンツを参照済みか
  ansEl: null, ansHead: null,   // 逐次表示中の回答カード本体/見出し
  liveTraceTree: null,   // trace_version=2 のときだけ張る階層描画ツリー
  sending: false,   // send() の開始POST応答待ち中（再入を拒否する）
};

// 組み込みの質問例（クリックで入力欄に流し込む・自動送信しない）。
export const DEFAULT_EXAMPLES = [
  '消費税率を変更すると、影響がありそうな箇所を教えてください。',
  '夜間バッチが異常終了しました。原因の候補を教えてください。',
  '消費税の端数処理の仕様を教えてください。',
  '登録されている資料の内容を要約した概要資料を作ってください。',
];

// 実際に描画する質問例。配列参照は不変で、setChatExamples() が中身だけ差し替える。
export const EXAMPLES = [...DEFAULT_EXAMPLES];

// 管理者設定 `chat_examples` を EXAMPLES へ反映する。null＝組み込み既定のまま／配列＝差し替え（空配列＝非表示）。
export function setChatExamples(list) {
  EXAMPLES.length = 0;
  EXAMPLES.push(...(list == null ? DEFAULT_EXAMPLES : list));
}
