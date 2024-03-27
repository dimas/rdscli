import boto3
import botocore
import time
import signal
import subprocess
import re
import json
import shlex
import os
import argparse

# TODO
# botocore.exceptions.ClientError: An error occurred (ValidationError) when calling the UpdateStack operation: Stack:arn:aws:cloudformation:eu-west-1:..... is in ROLLBACK_COMPLETE state and can not be updated.


# TODO: when stack fails to update:
# botocore.exceptions.WaiterError: Waiter StackUpdateComplete failed: Waiter encountered a terminal failure state: For expression "Stacks[].StackStatus" we matched expected path: "UPDATE_ROLLBACK_COMPLETE" at least once

ssm_client = boto3.client('ssm')  
cf_client = boto3.client('cloudformation')  
autoscaling_client = boto3.client('autoscaling')


def delete_stack(stack_name):
    cf_client.delete_stack(
        StackName=stack_name
    )
    waiter = cf_client.get_waiter('stack_delete_complete')
    print("...waiting for stack to be deleted...")
    waiter.wait(StackName=stack_name)


def ensure_stack(stack_name, template):

    stacks = cf_client.describe_stacks(StackName=stack_name).get('Stacks')
    if len(stacks) == 0:

        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudformation/client/create_stack.html

        r = cf_client.create_stack(
            StackName=stack_name,
            TemplateBody=template,
            TimeoutInMinutes=5,
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
        )

        print(f'create_stack => {r}')

        waiter = cf_client.get_waiter('stack_create_complete')

    else:

        # Stack already exists, update it
        print(f"Stack {stack_name} already exists, attempting to update...")

        try:
            r = cf_client.update_stack(
                StackName=stack_name,
                TemplateBody=template,
                TimeoutInMinutes=5,
                Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
            )

            print(f'update_stack => {r}')

            waiter = cf_client.get_waiter('stack_update_complete')

        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Message'] != 'No updates are to be performed.':
                raise

            print(f"Stack {stack_name} is up to date.")
            return

    print("...waiting for stack to be ready...")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={
            'Delay': 1
        }
    )


def show_stack_events(stack_name):
    r = cf_client.describe_stack_events(StackName=stack_name)
    for i in r['StackEvents']:
      print('-----------------------------------')
      print(i.get('Timestamp'), i.get('LogicalResourceId'), i.get('ResourceStatus'), i.get('ResourceStatusReason'))


def get_stack_outputs(stack_name):
    stacks = cf_client.describe_stacks(StackName=stack_name).get('Stacks')
    if len(stacks) != 1:
        raise Exception(f'Wrong number of stacks: {len(stacks)}')

    return stacks[0].get('Outputs')


def find_output(outputs, key):
    return next((o['OutputValue'] for o in outputs if o.get('OutputKey') == key), None)


def acquire_instance(stack_name):

    last_announce = None

    def announce_waiting(text):
        nonlocal last_announce
        if last_announce != text:
            if last_announce != None:
                print()
            print(f'{text}: ', end='', flush=True)
            last_announce = text

    outputs = get_stack_outputs(stack_name)

    asg = find_output(outputs, 'AutoScalingGroup')

    start = time.time()

    while True:
        response = autoscaling_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg],
        )

        autoscaling_groups = response.get('AutoScalingGroups')
        if len(autoscaling_groups) != 1:
            raise Exception(f'Wrong number of groups: {len(autoscaling_groups)}')

        instances = autoscaling_groups[0].get('Instances')

        instances = [i for i in instances if i.get('LifecycleState') == 'InService']

        if len(instances) == 0:
            announce_waiting(f'Waiting for an instance in ASG')

        elif len(instances) > 1:
            raise Exception(f'Unexpected number of instances: {len(instances)}')

        else:

            instance_id = instances[0].get('InstanceId')

            announce_waiting(f'Waiting for {instance_id} to respond')

            if ping_instance(instance_id):
                print()
                return instance_id

        time_spent = int(time.time() - start)
        if time_spent > 90:
            raise Exception('Timed out waiting for a proxy instance')

        print('.', end='', flush=True)
        time.sleep(1)
        

def run_command(instance_id, commands, timeout_seconds = 60):
    response = ssm_client.send_command(
        InstanceIds=[instance_id],
        DocumentName='AWS-RunShellScript',
        TimeoutSeconds=timeout_seconds,
        Parameters={
            'commands': commands
        }
    )

    command_id = response.get('Command').get('CommandId')

    waiter = ssm_client.get_waiter('command_executed')
    waiter.wait(
        CommandId=command_id,
        InstanceId=instance_id,
        WaiterConfig={
            'Delay': 0.5
        }
    )

    response = ssm_client.get_command_invocation(
        CommandId=command_id,
        InstanceId=instance_id
    )

    print(response.get('Status'))
    print(response.get('StandardOutputContent'))


def ping_instance(instance_id):
    try:
        run_command(instance_id, ['echo'], timeout_seconds = 30)
        return True
    except ssm_client.exceptions.InvalidInstanceId as e:
        # When instance hasn't fully initialised, we get this:
        #     An error occurred (InvalidInstanceId) when calling the SendCommand operation: Instances [[i-0123456789abcdef0]] not in a valid state for account 123456789012
        return False 


def get_secret(secret_name):
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secret_name)
    return response['SecretString']


def invoke_function(function_name, payload):
    lambda_client = boto3.client('lambda')
    response = lambda_client.invoke(
        FunctionName=function_name,
        Payload=json.dumps(payload),
    )

    # Lambda invocation response: https://docs.aws.amazon.com/lambda/latest/dg/API_Invoke.html#API_Invoke_ResponseElements
    status_code = response.get('StatusCode')
    function_error = response.get('FunctionError')
    payload = response.get('Payload')

    # In case of errors in Python lambda functions,
    #   {'StatusCode': 200, 'FunctionError': 'Unhandled', 'ExecutedVersion': '$LATEST', 'Payload': ...}
    # and Payload is a JSON with:
    #   errorType (exception), errorMessage, stackTrace (array) and requestId

    response_data = {}
    parse_error = None

    if payload is not None:
        try:
            response_data = json.loads(payload.read())
        except json.JSONDecodeError as e:
            parse_error = e

    if status_code != 200 or function_error or not payload:
        raise Exception(f'unsuccessful lambda invocation: StatusCode={status_code}, FunctionError={function_error}, errorMessage={response_data.get("errorMessage")}')

    if parse_error is not None:
        raise Exception(f'invalid lambda response: {payload}') from payload_parse_error

    return response_data


def open_tunnel_cli(instance_id, host, port):
    parameters = {
        'host': [host],
        'portNumber': [str(port)],
        'localPortNumber': ['0'],
    }

    cmdline = ['aws',
        'ssm', 'start-session',
        '--document-name', 'AWS-StartPortForwardingSessionToRemoteHost',
        '--parameters', json.dumps(parameters),
        '--target', instance_id
    ]

    logcmd = ' '.join(map(shlex.quote, cmdline))
    print(f'# {logcmd}')

    local_port = None

    proc = subprocess.Popen(cmdline, stdout=subprocess.PIPE, start_new_session=True)
    for line in proc.stdout:
        if match := re.search('^Port (\d+) opened for sessionId ', line.decode()):
            local_port = int(match.group(1))
            break

    print(f'Tunnel ready. Local port: {local_port}')

    return local_port, proc


def close_tunnel_cli(proc):
    # Kill the tunnel - proc.terminate() is not enough as it just kills 'ssm start-session' process,
    # but not 'session-manager-plugin' it spawns, so kill the entire process group.
    # Alternative is psutil.Process(pid).children(recursive=True) but that is an extra dependency...
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


def mysql_cli(local_port, db):
    cmdline = ['mysql',
        f'--host=127.0.0.1',
        f'--port={local_port}',
        f'--user={db["username"]}',
        f'--password={db["password"]}',
        db["dbname"]
    ]

    filtered_cmdline = ['--password=...' if i.startswith('--password=') else i for i in cmdline]
    logcmd = ' '.join(map(shlex.quote, filtered_cmdline))
    print(f'# {logcmd}')

    # Prevent Python from handling Ctrl+C, let the mysql itself deal with it
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    subprocess.run(cmdline)

    signal.signal(signal.SIGINT, signal.SIG_DFL)


###########################################

def read_file(file_name):
    with open(file_name) as f:
        return f.readlines()


def resolve_includes(line):
    match = re.search(r'^ (.*) \{\{ INCLUDE : (\S+) \}\} (.*) $', line, re.VERBOSE)
    if not match:
        return line

    indent = match.group(1)
    file_name = match.group(2)
    trailer = match.group(3)

    if trailer != '':
        raise Exception(f'Invalid use of INCLUDE for {file_name}: must be at the end of line')

    if not re.match(r'^\s*$', indent):
        raise Exception(f'Invalid use of INCLUDE for {file_name}: only indent whitespace must precede')

    if '/' in file_name:
        raise Exception(f'Invalid use of INCLUDE for {file_name}: no directory path is allowed')

    file_name = 'files/' + file_name
    if not os.path.isfile(file_name):
        raise Exception(f'Invalid use of INCLUDE for {file_name}: file not found')

    return read_file_with_includes(file_name, indent)


def read_file_with_includes(file_name, indent = ''):
    result = ''
    for i in read_file(file_name):
        result += resolve_includes(indent + i)
    return result


###########################################

def main():

    stack_name = 'dmitry-test'

    parser = argparse.ArgumentParser(
        description='Command-line client for RDS'
    )

    parser.add_argument('--secret', metavar='NAME', required=True,
                        help='name of a secret in AWS Secrets Manager with RDS credentials')
    #parser.add_argument('args', nargs=argparse.REMAINDER,
    #                    help='additional arguments to pass to client')

    args = parser.parse_args()

    print(f'Reading RDS credentials: {args.secret}')
    db = json.loads(get_secret(args.secret))

    start = time.time()
    print('Deploying proxy service')

    template = read_file_with_includes('files/template.yaml')

    print(template)

    ensure_stack(stack_name, template)

    print(f'Service deployed in {int(time.time() - start)}s')

    outputs = get_stack_outputs(stack_name)

    control_function = find_output(outputs, 'ControlLambdaFunction')

    print('Requesting proxy activation')
    invoke_function(control_function, {'Action': 'activate'})

    start = time.time()

    instance_id = acquire_instance(stack_name)
    print(f'Instance {instance_id} acquired in {int(time.time() - start)}s')

    local_port, proc = open_tunnel_cli(instance_id, db['host'], 3306)

    mysql_cli(local_port, db)

    close_tunnel_cli(proc)


main()
