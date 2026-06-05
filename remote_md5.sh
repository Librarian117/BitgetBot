#!/bin/bash
cd /root/BitgetBot
for f in *.py; do
    if [ -f "$f" ]; then
        h=$(md5sum "$f" 2>/dev/null | awk '{print $1}')
        echo "$f $h"
    fi
done
