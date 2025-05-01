import json

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
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create IAM role for Step Functions
        sfn_role = iam.Role(
            self, "PRReviewerStepFunctionRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com")
        )

        # Step 1: Define the secret (if it doesn't already exist)
        secret = secretsmanager.Secret.from_secret_name_v2(
            self,
            "ExistingStripeSecret",
            secret_name="dev/github_token",  # Name of the existing secret
        )

        # Add permissions for Bedrock
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                ],
                resources=["*"]
            )
        )
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "lambda:InvokeFunction"
                ],
                resources=[
                    "*"
                ]
            )
        )

        # Create Lambda function for PR review
        pr_review_lambda = PythonFunction(
            self, "PRReviewFunction",
            entry="lambda",
            index="handler.py",
            handler="lambda_handler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            environment={

                "POWERTOOLS_SERVICE_NAME": "pr-reviewer",
                "POWERTOOLS_METRICS_NAMESPACE": "PRReviewer",
                "LOG_LEVEL": "INFO"
            },
            timeout=Duration.seconds(60)
        )

        secret.grant_read(pr_review_lambda)
        # Load the ASL definition from the JSON file
        with open("./state_machine/github_review_workflow.asl.json", "r") as file:
            state_machine_definition = json.load(file)

        # Create Step Functions workflow
        workflow = sfn.StateMachine(
            self, "PRReviewerWorkflow",
            definition_body=sfn.DefinitionBody.from_string(
                json.dumps(state_machine_definition)
            ),

            definition_substitutions={

                "INVOKE_LAMBDA_FUNCTION_ARN": pr_review_lambda.function_arn,
            },
            # Use definition_body
            state_machine_type=sfn.StateMachineType.STANDARD,

            role=sfn_role
        )

        # Create API Gateway
        api = apigateway.RestApi(
            self, "PRReviewerApi",
            rest_api_name="PR Reviewer API",
            description="API for AI-powered PR reviews",
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=100,
                throttling_burst_limit=50
            )
        )

        # Create API key and usage plan
        api_key = apigateway.ApiKey(
            self, "PRReviewerApiKey",
            api_key_name="PR-Reviewer-Key",
            description="API key for PR Reviewer service"
        )

        usage_plan = apigateway.UsagePlan(
            self, "PRReviewerUsagePlan",
            name="PR-Reviewer-Plan",
            description="Usage plan for PR Reviewer API",
            api_stages=[
                apigateway.UsagePlanPerApiStage(
                    api=api,
                    stage=api.deployment_stage
                )
            ],
            throttle=apigateway.ThrottleSettings(
                rate_limit=100,
                burst_limit=50
            ),
            quota=apigateway.QuotaSettings(
                limit=1000,
                period=apigateway.Period.MONTH
            )
        )

        usage_plan.add_api_key(api_key)

        # Create Lambda function for API Gateway integration
        api_lambda = PythonFunction(
            self, "ApiHandler",
            entry="lambda",
            index="api_handler.py",
            handler="lambda_handler",
            runtime=_lambda.Runtime.PYTHON_3_11,
            environment={
                "POWERTOOLS_SERVICE_NAME": "pr-reviewer-api",
                "POWERTOOLS_METRICS_NAMESPACE": "PRReviewer",
                "LOG_LEVEL": "INFO"
            },
            timeout=Duration.seconds(30)
        )

        api_lambda.add_environment("STATE_MACHINE_ARN",workflow.state_machine_arn)

        # Grant permissions to invoke Step Functions
        api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution", "states:DescribeExecution"],
                resources=[workflow.state_machine_arn]
            )
        )

        # Add X-Ray and CloudWatch permissions
        api_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData"
                ],
                resources=["*"]
            )
        )

        # Create API Gateway resources and methods
        reviews = api.root.add_resource("review")
        status = api.root.add_resource("status").add_resource("{execution_arn}")

        # POST /review
        reviews.add_method(
            "POST",
            apigateway.LambdaIntegration(api_lambda),
            api_key_required=True
        )

        # GET /status/{execution_arn}
        status.add_method(
            "GET",
            apigateway.LambdaIntegration(api_lambda),
            api_key_required=True
        )

        # Output the API key and endpoint
        self.api_endpoint = api.url
        self.api_key = api_key.key_id 