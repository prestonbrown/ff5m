#!/bin/bash

# shellcheck disable=SC1091
source /opt/config/mod/.shell/common.sh

if  [ "$1" == 1 ]
    then
        # :? aborts rather than running "rm -rf /*" if the descriptor is missing.
        rm -rf "${LOG_DIR:?}"/*
        rm -rf /opt/config/mod_data/log/*
        sync
fi

if  [ "$2" == 1 ]
    then
        find /data/ -type f -not -regex "/data/lost+found/.*" -not -regex "/data/\.mod/.*" -not -regex "${LOG_DIR}.*" -exec rm {} \;
        sync
        find /data/ -type d -not -regex "/data/\.mod.*"  -not -regex "/data/lost+found.*" -not -path "/data/" -not -path "$LOG_DIR" -exec rm -r {} \;
        sync
fi
