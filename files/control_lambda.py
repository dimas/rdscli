import os
import boto3
from datetime import datetime, timezone


autoscaling_client = boto3.client('autoscaling')

asg = os.environ['AUTOSCALING_GROUP']

TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'

def set_asg_tag(name, value):
    autoscaling_client.create_or_update_tags(
        Tags=[
            {
                'ResourceId': asg,
                'ResourceType': 'auto-scaling-group',
                'Key': name,
                'Value': value,
                'PropagateAtLaunch': False
            },
        ]
    )


def get_asg_tags():
    response = autoscaling_client.describe_tags(
        Filters=[
            {'Name': 'auto-scaling-group', 'Values': [asg]},
        ],
    )

    return response.get('Tags')


def find_tag(tags, key):
    return next((t['Value'] for t in tags if t.get('Key') == key), None)


def utcnow():
    return datetime.now(timezone.utc)


def parse_utc(text):
    if text is None:
        return None
    try:
        return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_utc():
    return utcnow().strftime(TIMESTAMP_FORMAT)


def cleanup_if_idle():
    # If there were no activity for 1 hour and no recent requests, terminate EC2 instance

    tags = get_asg_tags()

    last_activity = parse_utc(find_tag(tags, 'LastActivity'))
    last_request = parse_utc(find_tag(tags, 'LastRequest'))
    first_cleanup = parse_utc(find_tag(tags, 'FirstCleanup'))

    print(f'Activity check: last_activity={last_activity}, last_request={last_request}, first_cleanup={first_cleanup}')

    if first_cleanup is None:
        set_asg_tag('FirstCleanup', format_utc())

    # When cleanup is triggered on a freshly deployed stack, there may be neither LastActivity nor LastRequest present yet.
    # In that case calculate idle time off FirstCleanup and do not do any cleanup if it is missing too (brand new stack)!
    times = [t for t in [last_activity, last_request, first_cleanup] if t is not None]               
    if len(times) == 0:
        return

    activity = max(times) 

    idle_seconds = (utcnow() - activity).seconds
    print(f'Inactivity estimate: {idle_seconds}s')

    if idle_seconds < 10 * 60:
        return

    print('Inactive for too long, terminating EC2 instance')
    autoscaling_client.set_desired_capacity(
        AutoScalingGroupName=asg,
        DesiredCapacity=0,
    )


def handler(event, context):
    action = event.get('Action', None)

    print(f'Event: {event}')

    if action == 'report':
        # Proxy EC2 instance is checking in reporting number of active sessions.

        if event.get('ActiveSessions', None) > 0:
            set_asg_tag('LastActivity', format_utc())
        else:
            cleanup_if_idle()

    elif action == 'activate':
        # Client-side script requests a tunnel so we need to bring EC2 instance back to life if it was terminated.

        set_asg_tag('LastRequest', format_utc())

        autoscaling_client.set_desired_capacity(
            AutoScalingGroupName=asg,
            DesiredCapacity=1,
        )

    elif action == 'cleanup':
        # Scheduled operation to check if we still need our resources or it can be released.
        # Triggered from outside of our EC2 instance so gets invoked even when instance is terminated.
        cleanup_if_idle()

    else:
        raise Exception(f'Invalid action: {action}')

