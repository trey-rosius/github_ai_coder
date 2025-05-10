import aws_cdk as cdk
from aws_cdk import (
    Stack,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_secretsmanager as secretsmanager,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_apigateway as apigateway,
    Duration,
)
from aws_cdk.aws_lambda_python_alpha import PythonFunction
from constructs import Construct

class GithubAiCoderStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # 1) Role for Step Functions
        sfn_role = iam.Role(
            self, "PRReviewerStepFunctionRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            description="Allows Step Functions to invoke Bedrock and Lambda"
        )


        '''
        # Bedrock permissions (no change; if you know the ARNs you can lock down 'resources' further)
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))  
        
        '''

        # 2) Import existing GitHub token secret
        gh_token = secretsmanager.Secret.from_secret_name_v2(
            self, "GitHubTokenSecret",
            secret_name="dev/github_token",
        )

        # 3) PR-review Lambda
        pr_review_fn = PythonFunction(
            self, "PRReviewFunction",
            entry="lambda/",             # your code folder
            index="pr_review_handler.py",
            handler="lambda_handler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            timeout=Duration.minutes(10),
            environment={
                "POWERTOOLS_SERVICE_NAME": "pr-reviewer",
                "POWERTOOLS_METRICS_NAMESPACE": "PRReviewer",
                "LOG_LEVEL": "INFO",
            },
        )

        # 3) PR-review Lambda
        notify_slack_fn = PythonFunction(
            self, "NotifySlackFunction",
            entry="lambda/",             # your code folder
            index="notify_slack_handler.py",
            handler="lambda_handler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            timeout=Duration.minutes(1),
            environment={
                "POWERTOOLS_SERVICE_NAME": "pr-reviewer",
                "POWERTOOLS_METRICS_NAMESPACE": "PRReviewer",
                "LOG_LEVEL": "INFO",
                "SLACK_WEBHOOK_URL":"https://hooks.slack.com/services/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"
            },
        )

        # Grant it read access to the GitHub token
        gh_token.grant_read(pr_review_fn)

        # Allow the Lambda to call Bedrock
        pr_review_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            resources=["*"],
        ))

        # Now let Step Functions invoke the PR-review Lambda (tighten resource to this function only)
        sfn_role.add_to_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[pr_review_fn.function_arn, notify_slack_fn.function_arn],
        ))

        # 4) State Machine
        # Load directly from file (no need to `open`/`json.load` yourself)
        definition = sfn.DefinitionBody.from_file("state_machine/github_review_workflow.asl.json")

        workflow = sfn.StateMachine(
            self, "PRReviewerWorkflow",
            definition_body=definition,
            definition_substitutions={
                "INVOKE_PR_REVIEW_FUNCTION_ARN": pr_review_fn.function_arn,
                "INVOKE_NOTIFY_SLACK_FUNCTION_ARN": notify_slack_fn.function_arn,
            },
            role=sfn_role,
            state_machine_type=sfn.StateMachineType.STANDARD,
        )

        # 5) API Gateway + Usage Plan
        api = apigateway.RestApi(
            self, "PRReviewerApi",
            rest_api_name="PR Reviewer API",
            description="API for AI-powered PR reviews",
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=50,
            ),
        )

        api_key = api.add_api_key("PRReviewerApiKey",
            api_key_name="PR-Reviewer-Key",
            description="API key for PR Reviewer service"
        )

        usage_plan = api.add_usage_plan("PRReviewerUsagePlan",
            name="PR-Reviewer-Plan",
            throttle=apigateway.ThrottleSettings(rate_limit=100, burst_limit=50),
            quota=apigateway.QuotaSettings(limit=1000, period=apigateway.Period.MONTH),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(api=api, stage=api.deployment_stage)

        # 6) Lambda for API backing
        api_handler = PythonFunction(
            self, "ApiHandler",
            entry="lambda/",            # same folder or separate?
            index="api_handler.py",
            handler="lambda_handler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            timeout=Duration.seconds(30),
            environment={
                "POWERTOOLS_SERVICE_NAME": "pr-reviewer-api",
                "POWERTOOLS_METRICS_NAMESPACE": "PRReviewer",
                "LOG_LEVEL": "INFO",
                "STATE_MACHINE_ARN": workflow.state_machine_arn,
            },
        )

        # Grant it right to start & describe our state machine only
        workflow.grant_start_execution(api_handler)
        api_handler.add_to_role_policy(iam.PolicyStatement(
            actions=["states:DescribeExecution"],
            resources=["*"],
        ))

        # X-Ray + CloudWatch metrics
        api_handler.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "xray:PutTraceSegments",
                "xray:PutTelemetryRecords",
                "cloudwatch:PutMetricData",
            ],
            resources=["*"],
        ))

        # 7) Wire up REST endpoints
        review = api.root.add_resource("review")
        review.add_method(
            "POST",
            apigateway.LambdaIntegration(api_handler),
            api_key_required=True,
        )

        status = api.root.add_resource("status").add_resource("{sfnExecutionArn}")
        status.add_method(
            "GET",
            apigateway.LambdaIntegration(api_handler),
            api_key_required=True,
        )

        # 8) Exports (optional)
        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "ApiKey", value=api_key.key_id)
