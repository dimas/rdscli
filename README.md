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

It deploys all the necessary infrastructure (see below) including an EC2 instance used to forward TCP traffic to the RDS through and
monitors activity of that instance. When instance is idle for certain time, it is terminated. Next time the tool is run,
depending on the state of the proxy infrastructure, it will either re-use already available instance, or start a new one.

Approximate time required before `mysql` cient connects:
* about 2 minutes when `rdscli` is used for the first time and has to deploy the entire infrastructure. Or when infrastructure was removed and needs to be redeployed.
* about 30 seconds when the infrastructure is in place and only EC2 instance needs to be launched because the last one was terminated for inactivity
* less than 5 seconds if there is already an EC2 instance running (because `rdscli` was used recently)

## Requirements

You will need:
1. Python 3
2. Boto3 - AWS SDK for Python - https://github.com/boto/boto3
3. AWS CLI (`aws` command line tool) - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
4. Session Manager plugin - https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
5. `mysql` command-line client

## Use

The tool is run with
```sh
python3 rdscli.py --secret=...
```
There `--secret` option gives a name of secret in AWS Secrets Manager that contains RDS credentials.

The tool will use your default AWS credentials - what it will be depends on what environment variables you have (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) or if you have a default profile in `~/.aws/credentials`

You can always specify a profile you want to use with:
```sh
AWS_PROFILE=my_profile python3 rdscli.py ...
```

## Implementation

The `rdscli` deploys a Cloud Formation stack containing all the resources. This is to allow easy cleanup - by deleting that stack you can remove everything `rdscli` created in the cloud.

### Proxy EC2 instance 

EC2 instance acting as a jumphost/proxy - it is a bog standard Amazon Linux 2 AMI with no extra software on it. The AL2 machines have SSM agent running on them out of the box and this is how TCP port gets forwarded from a local machine to remote database.

In addition to that, `rdscli` places onto the EC2 instance a shell script that continuously monitors instance activity and reports it to control lambda. The purpose of it is to allow control lambda know when there are no more connections being forwarded through that instance so it can be safely rerminated to save costs.

### Auto scaling group

The EC2 instance is a member (the only!) of an ASG which is used just for easy control of that instance.
When proxy is needed, control lambda sets ASG size to 1 and AWS brings an instance up, when proxy is not needed anymore - the control lambda sets ASG size to 0 and AWS terminates the instance.

### Control lambda function

The lambda has two main responsibilities:

1. `rdscli` tells lambda when it needs an a connection so lambda starts a proxy instance if it is not running yet.
2. Proxy EC2 instance periodically reports its (in)activity to the lambda. When lambda determines instance is not needed anymore, it gets rid of it by setting auto scaling group size to zero.



