#!/bin/bash
# Auto lint + format + syntax check for Python files
# Called by PostToolUse hook on every Edit/Write
FILE="$CLAUDE_FILE_PATH"
if [[ "$FILE" == *.py ]]; then
    python -m ruff check --fix "$FILE" 2>/dev/null
    python -m ruff format "$FILE" 2>/dev/null
    python -c "compile(open('$FILE',encoding='utf-8').read(),'$FILE','exec')" 2>/dev/null && echo "  [ruff+py] OK" || echo "  SYNTAX ERROR in $FILE - fix and re-edit!"
fi
