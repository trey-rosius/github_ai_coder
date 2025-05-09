import json
import boto3
import os
from typing import Dict, Any
from requests import Response

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.utilities.typing import LambdaContext
from aws_lambda_powertools.utilities.parser.models import APIGatewayProxyEventModel
from pydantic import BaseModel, Field

# Initialize Powertools
logger = Logger()
tracer = Tracer()
metrics = Metrics()
app = APIGatewayRestResolver()

state_machine_arn = os.environ.get("STATE_MACHINE_ARN")

# Initialize AWS clients
sfn_client = boto3.client('stepfunctions')


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)


@app.post('/review')
@tracer.capture_method
def handle_review_request() -> Dict[str, Any]:
    """Handle POST /review request"""
    review_request = app.current_event.json_body

    logger.info("Starting PR review", extra={
        "repository": review_request['repository'],
        "pr_number": review_request['pull_request_number'],
        "owner": review_request['owner'],
        "branch": review_request['branch'],
        "pr_author": review_request['pr_author'],
        "pr_title": review_request['pr_title'],
        "pr_state": review_request['pr_state'],
        "pr_created_at": review_request['pr_created_at'],
        "commit_sha": review_request['commit_sha']

    })

    # Start Step Functions execution
    response = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        input=json.dumps({
            'repository': review_request['repository'],
            'pull_request_number': review_request['pull_request_number'],
            'owner': review_request['owner'],
            'branch': review_request['branch'],
            "pr_author": review_request['pr_author'],
            "pr_title": review_request['pr_title'],
            "pr_state": review_request['pr_state'],
            "pr_created_at": review_request['pr_created_at'],
            "commit_sha": review_request['commit_sha']
        })
    )

    logger.info(f"start step function response {response}")

    metrics.add_metric(name="ReviewStarted", unit="Count", value=1)

    return {
        'statusCode': 200,
        'body': json.dumps({
            'execution_arn': response['executionArn'],
            'status': 'started'
        })
    }


@app.get("/status/<execution_arn>")
@tracer.capture_method
def handle_status_request(execution_arn: str) -> Dict[str, Any]:
    """Handle GET /status/{execution_arn} request"""

    logger.info("Checking review status", extra={"execution_arn": execution_arn})

    response = sfn_client.describe_execution(executionArn=execution_arn)
    logger.info(f"review status response {response}")

    metrics.add_metric(name="StatusChecked", unit="Count", value=1)

    return {
        'statusCode': 200,
        'body': json.dumps({
            'status': response['status'],
            'output': json.loads(response.get('output', '{}')),
            'startDate': response['startDate'].isoformat(),
            'stopDate': response.get('stopDate', '').isoformat() if response.get('stopDate') else None
        })
    }
