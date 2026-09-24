#!/bin/bash
# nightly batch driver (テスト用フィクスチャ・アナライザ拡張 波3 レーン B)
export ENV=prod
. ./common.sh
java -jar app.jar
java -cp lib com.acme.BatchMain
./PAYROLL
sqlplus scott/tiger @load.sql
echo "$LOG_DIR"
cat <<EOF
heredoc body references $LOG_DIR but must not be scanned
EOF
echo done
