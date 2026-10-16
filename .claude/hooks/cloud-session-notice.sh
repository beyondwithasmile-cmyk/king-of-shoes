#!/usr/bin/env bash
# クラウドのコンテナで始まったセッションにだけ、PCにつながっていないことを知らせる。
# アプリの「新規」は、PCのリモートコントロールの接続先が消えていると黙ってクラウドで作られる。
# 気づかないまま進むのを防ぐため、最初の返答で状態と直し方を伝えさせる。
# 詳しい経緯と直し方は docs/remote-control.md。

[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
case "${CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE:-}" in
  *cloud*) ;;
  *) exit 0 ;;
esac

cat <<'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "【接続状態】このセッションはクラウドのコンテナで動いており、PC（C:\\KOS\\KING_OF_SHOES_SYSTEM）のリモートコントロールにはつながっていない。PCのファイル・ツール・PCだけにあるMCP（xapi など）は使えない。\n\n最初の返答では次をする:\n1. 冒頭の1行で「このチャットはクラウドで動いていて、PCにはつながっていません」と伝える。\n2. mcp__claude-code-remote__list_environments を呼ぶ。kind が bridge の環境があれば、その name を挙げて「PCの接続先は動いています。新規作成のときに接続先をそれに切り替えてください」と伝える。無ければ「PCのリモートコントロールが止まっています。直し方は docs/remote-control.md」と伝える。どちらも2〜3行に収める。\n3. そのうえで依頼に取りかかる。Googleスプレッドシート・Gmail・カレンダーなどコネクタで足りる依頼はそのまま進めてよい。PCが要る依頼は、PC上の結果を推測で作らず、PCで続ける必要があると伝える。\n\n旅行の予定に関する依頼では CLAUDE.md の旅行ルール（まず対象を聞く）が優先する。接続状態の1行は添えてよいが、旅行を特定するための調査はしない。"
  }
}
EOF
