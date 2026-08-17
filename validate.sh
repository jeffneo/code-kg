#!/usr/bin/env bash
# Validate every demo query against both corpora.
#
# Correctness is decided by the CLI's exit status and whether stdout parses as
# JSON - never by grepping output text. Two earlier versions of this check
# reported false failures because result payloads legitimately contain the
# strings "TransientError" and "CypherSyntaxError": the neo4j driver defines
# classes with those names, and the queries return them as data.
set -uo pipefail
cd "$(dirname "$0")"

declare -a CASES=(
  "a|sym:repo:github.com/neo4j/neo4j-python-driver:src/neo4j/_async/driver.py#AsyncDriver|repo:github.com/neo4j/neo4j-python-driver"
  "b|sym:repo:github.com/grafana/dskit:user/id.go#InjectOrgID|repo:github.com/grafana/dskit"
)
fail=0
for case in "${CASES[@]}"; do
  IFS='|' read -r corpus sym lib <<< "$case"
  printf "corpus %s:\n" "$corpus"
  for q in q1 q2 q3 q4 q5 q6 q7 q8 q9 q10 q11 q12 q13 q14 q15 q16; do
    case $q in
      q4)       args=(query "$q" --param "lib_repo=$lib") ;;
      q1|q2|q3|q11) args=(query "$q" --param "changed=$sym") ;;
      q9|q10)   args=(query "$q" --param "corpus=$corpus") ;;
      q16)      args=(query "$q" --param "changed=$sym") ;;
      *)        args=(query "$q") ;;
    esac
    out=$(docker compose run --rm -T loader "${args[@]}" </dev/null 2>/dev/null | grep -vE "^ Container|^time=")
    status=$?
    rows=$(printf '%s' "$out" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null)
    if [[ $status -ne 0 || -z "$rows" ]]; then
      printf "  %-3s FAIL (exit=%s, unparseable output)\n" "$q" "$status"; fail=1
    else
      printf "  %-3s ok   %s rows\n" "$q" "$rows"
    fi
  done
done
exit $fail
