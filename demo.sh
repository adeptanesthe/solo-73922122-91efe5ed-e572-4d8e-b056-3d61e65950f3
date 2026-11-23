#!/usr/bin/env bash
# 口述史馆检索 API 演示脚本
# 场景：
#   1) 首查 → 撤稿 → 续页（跳过被撤版本、补足当页）
#   2) 同号重刊 → 旧游标看不到重刊版 / 新检索能看到
#   3) 撤稿不必等计分结束
#   4) 索引升级：模拟升级前旧库（索引未补齐），升级前签发的游标续页仍可用；
#      续页先走原路径，补齐后同一游标自动切到索引路径，只读命中版本的词频与稿长
#   5) 切到索引后，补齐期间的刊撤仍按旧游标规则生效
#   （补齐出错/重启续补由 tests/test_api.py 与 README 的独立探测覆盖）
# 用法：先启动服务（docker compose up --build 或本地 uvicorn），再执行 ./demo.sh
# 注意：脚本按空库设计；重复演示请 docker compose down -v 或换一个新 DB。
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8000}"

post() { # path [body]
  local path="$1" body="${2:-}"
  if [ -z "$body" ]; then
    curl -sS -X POST "$BASE$path"
  else
    curl -sS -X POST "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  fi
}

publish() { # doc_id tokens-json
  post /manuscripts "$(jq -nc --arg d "$1" --argjson t "$2" '{doc_id:$d, tokens:$t}')"
}

hr() { echo; echo "================================================================"; }
require_jq() { command -v jq >/dev/null || { echo "需要 jq"; exit 1; }; }
require_jq

echo "目标服务: $BASE"

hr
echo "1) 编辑刊发 7 篇分词后的转写稿（稿号 MS-001..MS-007）"
publish MS-001 '["抗战","北平","胡同","童年","粮票"]'        | jq -c .
publish MS-002 '["抗战","上海","弄堂","工厂","夜校"]'        | jq -c .
publish MS-003 '["北平","茶馆","曲艺","抗战","胜利","广播"]' | jq -c .
publish MS-004 '["知青","下乡","北大荒","麦田"]'             | jq -c .
publish MS-005 '["抗战","西南联大","昆明","北平","师生"]'    | jq -c .
publish MS-006 '["北平","城墙","骆驼","运输队"]'             | jq -c .
publish MS-007 '["抗战","伤兵","医院","护士"]'               | jq -c .

hr
echo "2) 在刊稿号重复刊发 → 409"
code=$(curl -sS -o /tmp/dup.json -w '%{http_code}' -X POST "$BASE/manuscripts" \
  -H 'Content-Type: application/json' -d '{"doc_id":"MS-001","tokens":["抗战"]}')
echo "HTTP $code $(cat /tmp/dup.json)"

hr
echo "3) 首查：terms=[抗战, 北平]，页长 2（先看一下页长 100 的完整榜单作对照）"
post /search '{"terms":["抗战","北平"],"size":100}' | jq '{total_hits, hits:[.hits[]|{doc_id,version,score}]}'
echo "--- 页长 2 的首查 ---"
first=$(post /search '{"terms":["抗战","北平"],"size":2}')
echo "$first" | jq '{total_hits, hits, next_cursor}'
cursor=$(echo "$first" | jq -r .next_cursor)

hr
echo "4) 馆方撤掉 MS-003（原本第 3 名，将在第 2 页出现）"
post /manuscripts/MS-003/withdraw '' | jq -c .

hr
echo "5) 凭游标续页：MS-003 被跳过，MS-006/MS-007 从后续命中补足当页，分数与首查一致"
page2=$(post /search/next "$(jq -nc --arg c "$cursor" '{cursor:$c}')")
echo "$page2" | jq '{remaining_hits, hits, next_cursor}'
if echo "$page2" | jq -e '[.hits[].doc_id] | index("MS-003") | not' >/dev/null \
   && [ "$(echo "$page2" | jq -r '[.hits[].doc_id]|join(",")')" = "MS-006,MS-007" ]; then
  echo "✓ 已撤稿的 MS-003 未出现，当页由后续命中补足"
else
  echo "✗ 续页结果不符合预期"; exit 1
fi
cursor2=$(echo "$page2" | jq -r .next_cursor)

hr
echo "6) 同号重刊 MS-003（新内容、新版本号）"
post /manuscripts "$(jq -nc '{doc_id:"MS-003", tokens:["北平","抗战","重逢","口述","整理"]}')" | jq -c .

hr
echo "7) 旧游标继续翻页：重刊版本不进入首查前已开的检索"
page3=$(post /search/next "$(jq -nc --arg c "$cursor2" '{cursor:$c}')")
echo "$page3" | jq '{remaining_hits, hits, next_cursor}'
if echo "$page3" | jq -e '[.hits[].doc_id] | index("MS-003") | not' >/dev/null; then
  echo "✓ 重刊的 MS-003 未进入旧检索"
else
  echo "✗ 旧检索不应看到重刊版本"; exit 1
fi

hr
echo "8) 重刊后新开的检索：能看到 MS-003 新版本"
fresh=$(post /search '{"terms":["抗战","北平"],"size":100}')
echo "$fresh" | jq '[.hits[]|{doc_id,version,score}]'
if echo "$fresh" | jq -e '.hits[] | select(.doc_id=="MS-003" and .version==8)' >/dev/null; then
  echo "✓ 新检索命中重刊后的 MS-003 (version=8)"
else
  echo "✗ 新检索未命中重刊版本"; exit 1
fi

hr
echo "9) 撤稿不必等正在计分的请求结束"
echo "   后台发起一个人为放慢 4 秒的首查，期间立即撤掉 MS-002"
post /search '{"terms":["抗战"],"size":100,"debug_delay_seconds":4}' > /tmp/slow_search.json &
slow_pid=$!
sleep 0.5   # 确认对方进入计分阶段
t0=$(date +%s%3N)
curl -sS -X POST "$BASE/manuscripts/MS-002/withdraw" | jq -c .
t1=$(date +%s%3N)
echo "   撤稿耗时 $((t1 - t0)) ms（远小于计分的 4000 ms，未被阻塞）"
wait $slow_pid
echo "   慢检索仍按其首查时刻的快照返回（MS-002 在结果中）："
jq -c '[.hits[].doc_id]' /tmp/slow_search.json

hr
echo "10) 索引升级：把当前库还原成「升级前旧库」（索引未补齐），并暂停后台补齐"
echo "--- 先补 12 篇与查询无关的在刊稿，用作「全库词频读取量」的对照"
for i in $(seq 1 12); do
  publish "MS-1$(printf '%02d' "$i")" "[\"知青\",\"下乡\",\"麦田\",\"编号$i\"]" >/dev/null
done
post '/index/rebuild?pause=true' '' | jq -c .
curl -sS "$BASE/index/status" | jq -c .
echo "--- 索引未就绪时首查，签发升级前游标 ---"
legacy_first=$(post /search '{"terms":["抗战","北平"],"size":2}')
echo "$legacy_first" | jq -c '{total_hits, hits:[.hits[].doc_id]}'
legacy_cursor=$(echo "$legacy_first" | jq -r .next_cursor)

echo "--- 升级前签发的游标：索引未就绪 → 续页走原路径，读取快照内全部在刊稿 ---"
post /search/next "$(jq -nc --arg c "$legacy_cursor" '{cursor:$c,debug:true}')" \
  | jq -c '{read_scope, remaining_hits, hits:[.hits[].doc_id]}'

hr
echo "11) 补齐期间照常刊发/撤稿/续页（无需等全库补完）"
post /manuscripts '{"doc_id":"MS-008","tokens":["北平","胡同","抗战","老街","冬储"]}' | jq -c .
post /manuscripts/MS-004/withdraw '' | jq -c .
echo "--- 暂停状态下手动推进一批（背景线程不参与，进度可复现）"
post '/index/backfill?batches=1' '' | jq -c .
curl -sS "$BASE/index/status" | jq -c '{indexed_seq,latest_version,ready}'
echo "--- 索引仍未就绪：升级前游标继续走原路径（此结果作为对照基准）"
scan_ref=$(post /search/next "$(jq -nc --arg c "$legacy_cursor" '{cursor:$c,debug:true}')")
echo "$scan_ref" | jq -c '{read_scope, remaining_hits, hits:[.hits[].doc_id]}'
if [ "$(echo "$scan_ref" | jq -r .read_scope.path)" = "scan" ]; then
  echo "✓ 补齐未完成，续页自动回退原路径"
else
  echo "✗ 索引未就绪却走了索引路径"; exit 1
fi

hr
echo "12) 继续补齐到「索引就绪」，同一升级前游标自动切到索引路径"
post /index/resume '' | jq -c .
post '/index/backfill?batches=100' '' | jq -c .
curl -sS "$BASE/index/status" | jq -c '{indexed_seq,latest_version,ready}'
idx_path=$(post /search/next "$(jq -nc --arg c "$legacy_cursor" '{cursor:$c,debug:true}')")
echo "$idx_path" | jq -c '{read_scope, remaining_hits, hits:[.hits[].doc_id]}'
if [ "$(echo "$scan_ref" | jq -c '[.hits[]|{doc_id,score}]')" = \
     "$(echo "$idx_path" | jq -c '[.hits[]|{doc_id,score}]')" ] \
   && [ "$(echo "$scan_ref" | jq .remaining_hits)" = "$(echo "$idx_path" | jq .remaining_hits)" ] \
   && [ "$(echo "$idx_path" | jq -r .read_scope.path)" = "index" ] \
   && [ "$(echo "$idx_path" | jq .read_scope.docs_scored)" -lt \
        "$(echo "$scan_ref" | jq .read_scope.docs_scored)" ]; then
  echo "✓ 切索引后分数/顺序/剩余命中数与原路径一致，且只读命中版本的词频与稿长"
  echo "  原路径读取 $(echo "$scan_ref" | jq .read_scope.docs_scored) 个在刊版本；" \
       "索引路径只读 $(echo "$idx_path" | jq .read_scope.docs_scored) 个命中版本（含已撤候选会被过滤）"
else
  echo "✗ 索引路径与原路径不一致"; exit 1
fi

hr
echo "13) 索引就绪后的稳态：新检索/续页全程走索引"
post /search '{"terms":["抗战","北平"],"size":100}' | jq -c '[.hits[]|{doc_id,version,score}]'
steady=$(post /search '{"terms":["抗战"],"size":2}')
steady_cursor=$(echo "$steady" | jq -r .next_cursor)
post /search/next "$(jq -nc --arg c "$steady_cursor" '{cursor:$c,debug:true}')" \
  | jq -c '{read_scope, hits:[.hits[].doc_id]}'

hr
echo "14) 稿件版本终态"
curl -sS "$BASE/manuscripts" | jq '[.manuscripts[]|{version,doc_id,status}]'
echo
echo "演示完成。"
