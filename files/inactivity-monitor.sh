#!/bin/bash

# Consinuously monitor how many SSM sessions are there by counting number of ssm-session-worker processes.
# Report activity as soon as it is detected and every 5 minutes.
# Checking process count every second may seem excessive but the goal is to report activity quickly
# when something opens SSM session. This EC2 instance does not have other things to do anyway
# so this kind of polling should not be a big deal.

last_report_sessions=0

while true
do
    now=$(date '+%s')
    since_report=$(( now - last_report ))

    sessions=$(ps h -C ssm-session-worker | wc -l)

    report_reason=

    if [ $(( now - last_report )) -ge 300 ]; then
        # Send report every 5 minutes regardless if we are active or not
        report_reason='periodic update'
    fi

    if [ $sessions -ge 1 -a $last_report_sessions -lt 1 ]; then
        # When activity is detected for the first time report it straight away.
        # This should prevent contol lambda from releasing the instance.
        report_reason='first session after inactivity'
    fi

    if [ -n "$report_reason" ]; then
        if [ $sessions -ge 1 ]; then
            active='true'
        else
            active='false'
        fi

        echo "Reporting - $report_reason: sessions=$sessions, active=$active"

        last_report=$now
        last_report_sessions=$sessions

        if [ -z "$region" ]; then
            region=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)
        fi

        aws --region="$region" \
            lambda invoke \
            --function-name sql-proxy-control \
            --payload "{\"Action\": \"report\", \"ActiveSessions\": $sessions, \"active\": $active}" \
            /dev/null
    fi

    sleep 1
done

