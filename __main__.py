from pulumi import Config, Output, export
import pulumi_aws as aws
import pulumi_awsx as awsx

config = Config()
# The port the app listens on. Hardcoded to 8080 in main.go, so it must match.
container_port = config.get_int("containerPort", 8080)
# The public port on the load balancer. The exported URL assumes this is 80.
listener_port = config.get_int("listenerPort", 80)
cpu =config.get_int("cpu", 128)
memory = config.get_int("memory", 128)

# An ECS cluster to deploy into
cluster = aws.ecs.Cluster("cluster")

vpc = awsx.ec2.DefaultVpc("default-vpc")
security_group = aws.ec2.SecurityGroup(
    "web-sg",
    vpc_id=vpc.vpc_id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=listener_port,
            to_port=listener_port,
            cidr_blocks=["0.0.0.0/0"],
        ),
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        ),
    ],
)

# Tasks only accept inbound traffic from the ALB. All outbound is allowed so they
# can pull the image from ECR, send logs, and reach example.com for /external-check.
task_security_group = aws.ec2.SecurityGroup(
    "task-sg",
    vpc_id=vpc.vpc_id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=container_port,
            to_port=container_port,
            security_groups=[security_group.id],
        ),
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        ),
    ],
)

# An ALB to serve the container endpoint to the internet
loadbalancer = awsx.lb.ApplicationLoadBalancer(
    "loadbalancer",
        listener=awsx.lb.ListenerArgs(
            port=listener_port,
        ),
        security_groups=[security_group.id],
        default_target_group=awsx.lb.TargetGroupArgs(
            port=container_port,
            protocol="HTTP",
            target_type="ip",
            vpc_id=vpc.vpc_id,
            # "/" deliberately returns a 500 on about 10% of requests. A high
            # unhealthy threshold stops those from killing healthy tasks (10 in
            # a row is about 1 in 10 billion), and a dead app is still caught
            # within 150s.
            health_check=aws.lb.TargetGroupHealthCheckArgs(
                path="/",
                port="traffic-port",
                matcher="200",
                interval=15,
                timeout=5,
                healthy_threshold=2,
                unhealthy_threshold=10,
            ),
        ),
)

# An ECR repository to store the application's container image
repo = awsx.ecr.Repository("repo", force_delete=True)

# Build and publish the application's container image from ./app to the ECR repository
image = awsx.ecr.Image(
    "image", repository_url=repo.url, context="./devops-assessment", platform="linux/amd64"
)

# Deploy an ECS Service on Fargate to host the application container
service = awsx.ecs.FargateService(
    "service",
    cluster=cluster.arn,
    # The public IP is for outbound traffic only: the default VPC has no NAT
    # gateway, and task_security_group blocks direct inbound connections.
    network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
        subnets=vpc.public_subnet_ids,
        security_groups=[task_security_group.id],
        assign_public_ip=True,
    ),
    health_check_grace_period_seconds=60,
    # Fail the deployment instead of replacing unhealthy tasks indefinitely.
    deployment_circuit_breaker=aws.ecs.ServiceDeploymentCircuitBreakerArgs(
        enable=True,
        rollback=True,
    ),
    task_definition_args={
        "container": {
            "name": "app",
            "image": image.image_uri,
            "cpu": cpu,
            "memory": memory,
            "essential": True,
            "port_mappings": [
                {
                    "container_port": container_port,
                    "target_group": loadbalancer.default_target_group,
                }
            ],
        },
    },
)

# The URL at which the container's HTTP endpoint will be available
export("url", Output.concat("http://", loadbalancer.load_balancer.dns_name))

# The name of the ECS cluster
export("clusterName", cluster.name)
