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
import sys

# Amazon Linux 2
DEFAULT_PROXY_AMI = 'ami-01d7b3abeb9d86b41'

# TODO
# botocore.exceptions.ClientError: An error occurred (ValidationError) when calling the UpdateStack operation: Stack:arn:aws:cloudformation:eu-west-1:..... is in ROLLBACK_COMPLETE state and can not be updated.


# TODO: when stack fails to update:
# botocore.exceptions.WaiterError: Waiter StackUpdateComplete failed: Waiter encountered a terminal failure state: For expression "Stacks[].StackStatus" we matched expected path: "UPDATE_ROLLBACK_COMPLETE" at least once

# TODO: terminated instance while still marked InService in ASG, running command results in
# botocore.exceptions.WaiterError: Waiter CommandExecuted failed: Max attempts exceeded. Previously accepted state: For expression "Status" we matched expected path: "Pending"

# TODO:
# Tunnel ready. Local port: None
# (no idea how I got to this state. redeployed stack, probably instances were restarting)
# ah! `aws ssm` failed with:
#   Setting up data channel with id dmitry.andrianov-0b03f8eab18ff00ca failed: failed to create websocket for datachannel with error: CreateDataChannel failed with no output or error: createDataChannel request failed: unexpected response from the service Server authentication failed: <UnauthorizedRequest><message>Forbidden.</message></UnauthorizedRequest>


ssm_client = None
cf_client = None
autoscaling_client = None
rds_client = None

def init_clients():
    global ssm_client, cf_client, autoscaling_client, rds_client

    ssm_client = boto3.client('ssm')
    cf_client = boto3.client('cloudformation')
    autoscaling_client = boto3.client('autoscaling')
    rds_client = boto3.client('rds')


def delete_stack(stack_name):
    cf_client.delete_stack(
        StackName=stack_name
    )
    waiter = cf_client.get_waiter('stack_delete_complete')
    print("...waiting for stack to be deleted...")
    waiter.wait(StackName=stack_name)


def get_stack(stack_name):
    try:
        stacks = cf_client.describe_stacks(StackName=stack_name).get('Stacks')
        return stacks[0] if len(stacks) > 0 else None
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Message'] == f'Stack with id {stack_name} does not exist':
            return None
        raise


def ensure_stack(stack_name, template, parameters):

    parameters = [{'ParameterKey': k, 'ParameterValue': v} for k, v in parameters.items()]

    stack = get_stack(stack_name)

    if stack is None:

        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudformation/client/create_stack.html

        print(f'Creating stack {stack_name}')

        cf_client.create_stack(
            StackName=stack_name,
            TemplateBody=template,
            Parameters=parameters,
            TimeoutInMinutes=5,
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
        )

        waiter = cf_client.get_waiter('stack_create_complete')

    else:

        # Stack already exists, update it
        print(f'Stack {stack_name} already exists, updating')

        try:
            cf_client.update_stack(
                StackName=stack_name,
                TemplateBody=template,
                Parameters=parameters,
                Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM'],
            )

            waiter = cf_client.get_waiter('stack_update_complete')

        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Message'] != 'No updates are to be performed.':
                raise

            print(f"Stack {stack_name} is up to date.")
            return

    print("Waiting for stack to be ready...")
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={
            'Delay': 1,
            'MaxAttempts': 5 * 60,
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
            if last_announce is not None:
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


# TODO
# botocore.exceptions.ClientError: An error occurred (ValidationError) when calling the UpdateStack operation: Stack:arn:aws:cloudformation:eu-west-1:000000000000:stack/tcp-proxy-5f7ede3a-325b8157/932764c0-1e89-11ef-a547-026fe946cb4b is in ROLLBACK_FAILED state and can not be updated.

def mysql_cli(local_port, username, password, database, args):
    cmdline = ['mysql',
        f'--host=127.0.0.1',
        f'--port={local_port}',
        f'--user={username}',
        f'--password={password}',
        *args,
        database
    ]

    filtered_cmdline = ['--password=...' if i.startswith('--password=') else i for i in cmdline]
    logcmd = ' '.join(map(shlex.quote, filtered_cmdline))
    print(f'# {logcmd}')

    # Prevent Python from handling Ctrl+C, let the mysql itself deal with it
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    subprocess.run(cmdline)

    signal.signal(signal.SIGINT, signal.SIG_DFL)


def rds_id_from_host(host):
    # <databaseid>.abcdefgwgxg2.eu-west-1.rds.amazonaws.com
    match = re.search(r'^ (.*) \. [a-z0-9]+ \. [a-z0-9-]+ \.rds\.amazonaws\.com\.? $', host, re.VERBOSE)
    return match.group(1) if match else None


def is_rds_host(host):
    return rds_id_from_host(host) is not None


def resolve_custom_db_host(host):
    import dns.resolver

    answer = dns.resolver.resolve(host, 'CNAME')

    # query() will throw when there are no records so we are checking for > 1 here really
    if len(answer) != 1:
        raise Exception(f'cannot resolve #{host} into RDS hostname')

    return str(answer[0])


def find_target_rds(host):
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/rds/client/describe_db_instances.html

    # We could iterate through all instances looking at their Endpoint.Address but lets save time using the fact that
    # DB hostname looks like
    #   <databaseid>.abcdefgwgxg2.eu-west-1.rds.amazonaws.com
    rds_instance_id = rds_id_from_host(host)
    if rds_instance_id is None:
        raise Exception(f'{host} is not an RDS hostname')

    response = rds_client.describe_db_instances(DBInstanceIdentifier=rds_instance_id)

    if len(response['DBInstances']) != 1:
        raise Exception(f'could not find RDS {rds_instance_id}')

    return response['DBInstances'][0]


def find_security_group(rds):
    vpc_sgs = rds.get('VpcSecurityGroups')
    if vpc_sgs is None or len(vpc_sgs) < 1:
        raise Exception(f'RDS does no have VpcSecurityGroups')

    if len(vpc_sgs) > 1:
        raise Exception(f'multiple groups in RDS VpcSecurityGroups')

    return vpc_sgs[0]['VpcSecurityGroupId']


def find_subnet(rds):
    subnets = rds['DBSubnetGroup']['Subnets']
    subnets = [s for s in subnets if s.get('SubnetStatus') == 'Active']

    # In my case I have got two subnets but in one of them proxy EC2 instance does not work.
    # The only difference is that one of subnets has default route 0.0.0.0/0 via NAT
    # and can talk to SSM and the other does not.
    # So my filtering here just searches for a subnet with default route.
    # Unfortunately, other setups can be completely different:
    #   * default route is not really required
    #   * ACL may also play a role
    # But I have no idea how to make anything universal (or just "more universal" here).

    ec2_client = boto3.client('ec2')

    for subnet in subnets:

        response = ec2_client.describe_route_tables(
            Filters=[
                {
                    'Name': 'association.subnet-id',
                    'Values': [ subnet['SubnetIdentifier'] ]
                }
            ]
        )

        for rt in response['RouteTables']:
            for route in rt['Routes']:
                if route['DestinationCidrBlock'] == '0.0.0.0/0':
                    # This RT has a default route, use it
                    return subnet['SubnetIdentifier']

    raise Exception(f'unable to find a suitable subnet')


def make_stack_id(group_id, subnet_id):
    return re.sub(r'^sg-', '', group_id) + '-' + re.sub(r'^subnet-', '', subnet_id)


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

    parser = argparse.ArgumentParser(
        description='Command-line client for RDS'
    )

    parser.add_argument('--secret-id', metavar='VALUE', required=True,
                        help='name of a secret in AWS Secrets Manager with RDS credentials')
    parser.add_argument('--instance-id', metavar='VALUE',
                        help='optional ID of an EC2 instance that will be used for tunnelling trafffic.')
    parser.add_argument('--group-id', metavar='VALUE',
                        help='ID of a security group for proxy EC2 instance. When omitted, try to infer it from RDS')
    parser.add_argument('--subnet-id', metavar='VALUE',
                        help='ID of a subnet to place proxy EC2 instance into. When omitted, try to infer it from RDS')
    parser.add_argument('args', nargs=argparse.REMAINDER,
                        help='additional arguments to pass to client')

    args = parser.parse_args()

    mysql_args = args.args
    if mysql_args and mysql_args[0] == '--':
        mysql_args = mysql_args[1:]

    init_clients()

    secret_id = args.secret_id

    print(f'Reading RDS credentials: {secret_id}')
    db = json.loads(get_secret(secret_id))

    # Make sure all the properties we need are present
    for n in ['host', 'username', 'password']:
        if db.get(n) is None:
            raise Exception(f'{secret_id} does not contain {n} attribute')

    db_host = db['host']
    db_port = db.get('port', 3306)
    db_username = db['username']
    db_password = db['password']
    db_name = db.get('dbname', 'mysql')
    db_engine = db.get('engine')

    if db_engine is not None and db_engine != 'mysql':
        raise Exception(f'{secret_id} points to non-MySQL RDS')

    print(f'DB host: {db_host}')

    instance_id = args.instance_id
    if instance_id is None:
        # Instance ID was not provided, need to deploy our own

        group_id = args.group_id
        subnet_id = args.subnet_id

        if group_id is None or subnet_id is None:
            if not is_rds_host(db_host):
                db_host = resolve_custom_db_host(db_host)
                if not is_rds_host(db_host):
                    raise Exception(f'resolved {host} is still not an RDS hostname')

            print(f'Resolved DB host: {db_host}')

            rds = find_target_rds(db_host)

            if group_id is None:
                group_id = find_security_group(rds)
                print(f'Security group: {group_id}')

            if subnet_id is None:
                subnet_id = find_subnet(rds)
                print(f'Subnet: {subnet_id}')

            print(f'Shortcut command:')
            print(f'#   {sys.argv[0]} --secret-id {secret_id} --group-id {group_id} --subnet-id {subnet_id}')


        start = time.time()
        print('Deploying proxy service')

        template = read_file_with_includes('files/template.yaml')

        stack_id = make_stack_id(group_id, subnet_id)
        stack_name = f'tcp-proxy-{stack_id}'

        stack_params = {
            'StackId': stack_id,
            'SecurityGroupId': group_id,
            'SubnetId': subnet_id,
            'ImageId': DEFAULT_PROXY_AMI,
        }

        ensure_stack(stack_name, template, stack_params)

        print(f'Service deployed in {int(time.time() - start)}s')

        outputs = get_stack_outputs(stack_name)

        control_function = find_output(outputs, 'ControlLambdaFunction')

        print('Requesting proxy activation')
        invoke_function(control_function, {'Action': 'activate'})

        start = time.time()

        instance_id = acquire_instance(stack_name)
        print(f'Instance {instance_id} acquired in {int(time.time() - start)}s')


    local_port, proc = open_tunnel_cli(instance_id, db_host, db_port)

    mysql_cli(local_port, db_username, db_password, db_name, mysql_args)

    close_tunnel_cli(proc)


main()
