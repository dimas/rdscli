# rdscli
CLI for AWS RDS databases

## Motivation

While AWS provides web-based query editor for its Aurora databases, there is no equivalent for "classic" RDS.

Accessing a MySQL RDS in a VPC usually requires using one of these approaches:
* either work from some EC2 instance on the same VPC where `mysql` client can be installed
* or create some sort of a tunnel that allows connecting to the RDS instance directly from a local client. Which in turn can be done with:
  * an EC2 instance inside VPC used as "jumphost" to tunnel TCP to the RDS through it
  * something more serious like a proper VPN or even more involved solutions

Where infrequent ad hoc access to database is needed, rolling out a proper VPN infrastructure is rarely justified and EC2 instance is a much simpler option.

However, you do not want to pay for that instance for extended time when it is only used rarely so it needs to be started and stopped
on demand.

## Overview

`rdscli` is a helper tool that builds a TCP tunnel into AWS VPC to allow locally running `mysql` client reach an RDS running in that VPC.
When tunnel is built, `rdscli` starts `mysql` client locally connecting it to the remote database.

The tool can either use an existing EC2 instance to forward RDS traffic through it, or it can deploy its own EC2 instance. In the later case, the tool then monitors activity of that instance. When instance is idle for certain time, it is automatically terminated. Next time `rdscli` is run, depending on the state of the proxy infrastructure, it will either re-use already available instance, or start a new one.

Approximate time needed for `mysql` cient to connects:
* 2-3 minutes when `rdscli` is used for the first time and has to deploy the entire infrastructure. Or when infrastructure was removed and needs to be redeployed.
* about 30 seconds when the infrastructure is already in place and only EC2 instance needs to be launched (because the previous one was terminated for inactivity)
* under 5 seconds if there is already an EC2 instance running (because `rdscli` was used recently) or you provide it with pre-existing instance

## Requirements

You will need:
1. Python 3
2. Boto3 - AWS SDK for Python - https://github.com/boto/boto3
3. AWS CLI (`aws` command line tool) - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
4. Session Manager plugin for `aws` - https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
5. `pip3 install dnspython`
6. `mysql` command-line client

## Use

The tool is run with
```sh
python3 rdscli.py --secret-id=...
```
There `--secret-id` option gives a name of secret in AWS Secrets Manager that contains RDS credentials.

The tool will use your default AWS credentials - what it will be depends on what environment variables you have (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) or if you have a default profile in `~/.aws/credentials`

You can always explicitly select a profile you want to use by setting environment variable in the same command:
```sh
AWS_PROFILE=my_profile python3 rdscli.py ...
```

## Parameters

If you already have an EC2 instance that has SSM agent installed and has connectivity to the RDS, you can just give ID of that instance to `rdscli` and it will be used for tunnelling traffic without need to deploy anything else.

```sh
python3 rdscli.py --secret-id=... --instance-id=...
```

This is quick but it is quite a niche use case - you probably won't be using this tool at all if you already had that jump host readily available. So when `--instance-id` is not specified, `rdscli` will create its own temporary EC2 proxy instance.

The tool needs to know two things in order to launch EC2 instance: security group and a subnet to place the instance into.
The subnet and group should allow the EC2 instance to talk to both RDS database and Amazon SSM control infrastructure.

You can explicitly provide these parameters via the command line:

```sh
python3 rdscli.py --secret-id=... --group-id=... --subnet-id=...
```

If you omit these parameters, `rdscli` will try to guess their values. That means fetching RDS configuration, checking what
security groups and subnets are there and trying to guess which one to use.
That piece of logic is quite dumb so it may not work for you given how different and how complex your VPC network setup can be.
There also may be network ACLs that will prevent the EC2 instance from communicating with SSM infrastructure even
if correct subnet/group is chosen.

There is, unfortunately, no remedy for that - if the tool cannot automatically find working subnet and group, you options are:
* either give the tool suitable existing subnet/group that should work
* or create new for the purpose
* put their creation into CloudFormation template (but you may need to extend it with additional parameters like VPC ID for that)
* improve the code that figures out correct subnet/group from the environment

## RDS credentials

The secret in Secrets Manager with RDS credentials is expected to be a JSON object with this format:
```json
{
    "engine": "mysql",
    "username": "...",
    "password": "..."
    "host": "my-database-endpoint.us-west-2.rds.amazonaws.com",
    "dbname": "myDatabase",
    "port": "3306"
}
```
which is a standard format for RDS secrets.

The `port` is not required - 3306 is assumed when it is absent. Also, `engine` is not needed but if present it is checked to be "mysql" or the tool will abort.

## Passing command-line options to RDS client

If you add a bare `--` to the command line, everything after it will be passed to the `mysql` program in addition to options added by `rdscli` automatically (which are host, port, credentials and database name). This can be used to execute a single command for example:

```sh
python3 rdscli.py --secret-id=... -- -B -e 'SELECT COUNT(*) FROM table'
```

## Cleanup

The EC2 instance is automatically terminated when not in use for some time. If you want to completely remove the tool's cloud
infrastructure, just detele its Cloud Watch stack (named `tcp-proxy-<unique ID>`) as everything is contained within that stack.

TODO: maybe add a command line option to the tool to remove cloud infrastructure.

## Permissions

Deployment of the cloud infrastructure requires user with lots of permissions. As one of the resources created is an IAM role,
it needs quite high admin-like access level.

However, after the initial deployment, significantly less permissions is needed to initiate the TCP tunnel.

## Implementation

The `rdscli` deploys a Cloud Formation stack containing all the resources. This is to allow easy cleanup - by deleting that stack you can remove everything `rdscli` created in the cloud.

### Proxy EC2 instance 

EC2 instance acting as a jumphost/proxy - started from a bog standard Amazon Linux 2 AMI with no extra software on it. The AL2 machines have SSM agent running on them out of the box and this is how TCP port gets forwarded from a local machine to remote database.

In addition to that, `rdscli` places onto the EC2 instance a shell script that continuously monitors instance activity and reports it to control lambda. The purpose of it is to allow control lambda know when there are no more connections being forwarded through that instance so it can be safely rerminated to save costs.

### Auto scaling group

The EC2 instance is wrapped into an ASG which is used just for easy control of that instance.
When proxy is needed, control lambda sets ASG size to 1 and AWS brings an instance up, when proxy is not needed anymore - the control lambda sets ASG size to 0 terminating the instance.

### Control lambda function

The lambda has two main responsibilities:

1. `rdscli` invokes lambda to tell it a connection is needed, so lambda starts a proxy instance if it is not running yet.
2. Proxy EC2 instance periodically reports its (in)activity to the lambda. When lambda determines instance is not needed anymore, it gets rid of the instance by setting auto scaling group size to zero.
