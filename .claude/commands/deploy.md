# Deploy Check & Sync

Run a full pre-deployment quality check, then sync to server.

## Step 1: Syntax Check (all .py files)
```bash
for f in *.py; do python -m py_compile "$f" && echo "OK $f" || echo "FAIL $f"; done
```

## Step 2: Import Test
```bash
python -c "import deepseek_quant_bot; print('Import OK')"
```

## Step 3: Check server bot status
```bash
ssh root@8.210.3.197 "ps aux | grep deepseek_quant | grep -v grep || echo 'NO_BOT_RUNNING'"
```

## Step 4: If all above pass, deploy
If steps 1-3 are all OK, run `.\sync.ps1` to sync changed files to the server and restart the bot. If the bot was running, it will be restarted automatically.

## Step 5: Verify deployment
After sync completes, wait 5 seconds, then check:
```bash
ssh root@8.210.3.197 "ps aux | grep deepseek_quant | grep -v grep && tail -10 /tmp/bot.log"
```

Report: files synced, bot PID, modules loaded, and any startup errors.
