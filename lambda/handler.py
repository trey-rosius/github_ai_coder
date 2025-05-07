import json
from typing import Dict, Any
from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError
from github.GithubException import GithubException
from utils import get_github_token, fetch_pr_changes, generate_review_with_bedrock, post_review_comments, PRReviewError

# Initialize Powertools
logger = Logger(service="pr-reviewer")
tracer = Tracer(service="pr-reviewer")
metrics = Metrics(namespace="PRReviewer")


@logger.inject_lambda_context(correlation_id_path=None)
@tracer.capture_lambda_handler
@metrics.log_metrics
def lambda_handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """
    Unified Lambda entry point routing actions to handlers with centralized validation
    and error handling.
    Supported actions: fetch_changes, generate_review, post_comments
    """

    def _response(status_code: int, payload: dict) -> dict:
        metrics.add_metric(name="Invocation", unit="Count", value=1)
        return {"statusCode": status_code, "body": json.dumps(payload)}

    action = event.get("action")
    if not action:
        logger.error("Missing 'action' in event")
        return _response(400, {"error": "Missing 'action' in event"})

    try:
        if action == "fetch_changes":
            # Validate required parameters
            missing = [k for k in ("repository", "pull_request_number", "owner") if not event.get(k)]
            if missing:
                msg = f"Missing parameters for fetch_changes: {missing}"
                logger.error(msg)
                return _response(400, {"error": msg})

            changes = fetch_pr_changes(
                repo_name=event["repository"],
                pr_number=int(event["pull_request_number"]),
                owner=event["owner"]
            )
            return _response(200, {"changes": changes})


        elif action == "generate_review":
            logger.info(f"changes are {event}")
            # Parse the JSON string in the 'changes' field
            changes_data = json.loads(event['changes'])
            changes_list = changes_data['changes']

            logger.info(f"changes list {changes_list}")
            if not isinstance(changes_list, list) or not changes_list:
                msg = "'changes' must be a non-empty list for generate_review"
                logger.error(msg)
                return _response(400, {"error": msg})

            reviews = generate_review_with_bedrock(changes_list)
            return _response(200, {"reviews": reviews})

        elif action == "post_comments":
            missing = [k for k in ("repository", "pull_request_number", "owner", "reviews") if not event.get(k)]
            if missing:
                msg = f"Missing parameters for post comments: {missing}"
                logger.error(msg)
                return _response(400, {"error": msg})

            result = post_review_comments(
                repo_name=event["repository"],
                pr_number=int(event["pull_request_number"]),
                owner=event["owner"],
                reviews_data=event["reviews"]
            )
            return _response(200, {"result": result})

        else:
            msg = f"Unsupported action: {action}"
            logger.error(msg)
            return _response(400, {"error": msg})

    except PRReviewError as pr_err:
        logger.exception("Handled PRReviewError")
        return _response(500, {"error": str(pr_err)})
    except GithubException as gh_err:
        logger.exception("GitHub API error")
        return _response(502, {"error": f"GitHub error: {gh_err}"})
    except (BotoCoreError, ClientError) as aws_err:
        logger.exception("AWS service error")
        return _response(502, {"error": f"AWS error: {aws_err}"})
    except Exception as err:
        logger.exception("Unexpected error in lambda_handler")
        return _response(500, {"error": "Internal server error"})
