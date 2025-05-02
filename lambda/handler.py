import json
import boto3
import os
from typing import Dict, Any, List, Optional
from botocore.config import Config
from github import Github
from github.GithubException import GithubException
from botocore.exceptions import BotoCoreError, ClientError

from aws_lambda_powertools import Logger, Tracer, Metrics
from aws_lambda_powertools.event_handler import APIGatewayRestResolver
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.parameters import SecretsProvider
from aws_lambda_powertools.utilities.typing import LambdaContext
from utils import get_github_token

# Initialize Powertools
logger = Logger()
tracer = Tracer()
metrics = Metrics()

# Constants
BEDROCK_MODEL_ID = 'anthropic.claude-3-5-sonnet-20240620-v1:0'
MAX_TOKENS = 1000
TEMPERATURE = 0.5
GITHUB_TOKEN_SECRET_NAME = "dev/github_token"

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('GitHubAIReviews')


class PRReviewError(Exception):
    """Custom exception for PR review related errors"""
    pass


def initialize_clients() -> tuple:
    """Initialize AWS and GitHub clients with error handling"""
    try:

        github_token = get_github_token()
        if not github_token:
            raise PRReviewError("Failed to retrieve GitHub token from Secrets Manager")

        github_client = Github(github_token)
        bedrock_client = boto3.client('bedrock-runtime')

        return github_client, bedrock_client
    except Exception as e:
        logger.error(f"Client initialization failed: {str(e)}")
        raise PRReviewError(f"Client initialization failed: {str(e)}")


github, bedrock = initialize_clients()


def fetch_pr_changes(repo_name: str, pr_number: int, owner: str) -> List[Dict[str, Any]]:
    """Fetch changes from a pull request with enhanced error handling"""
    try:
        logger.info(f"Fetching PR changes for repo: {owner}/{repo_name}, PR: {pr_number}")
        # we have to be sure that the pr_number is good
        repo = github.get_repo(f"{owner}/{repo_name}")
        pr_number = int(pr_number)
        pr = repo.get_pull(pr_number)
        changes = []

        logger.debug(f"Processing {pr.changed_files} files in PR")

        for file in pr.get_files():
            change_data = {
                'filename': file.filename,
                'status': file.status,
                'additions': file.additions,
                'deletions': file.deletions,
                'changes': file.changes
            }

            # Only include patch if it exists (for binary files it might be None)
            if file.patch:
                change_data['patch'] = file.patch

            changes.append(change_data)
            logger.debug(f"Processed file: {file.filename}, status: {file.status}")

        logger.info(f"Successfully fetched {len(changes)} files changes")
        return changes

    except GithubException as ge:
        error_msg = f"GitHub API error fetching PR changes: {str(ge)}"
        logger.error(error_msg, extra={"repo": repo_name, "pr": pr_number})
        raise PRReviewError(error_msg)
    except Exception as e:
        error_msg = f"Unexpected error fetching PR changes: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise PRReviewError(error_msg)


def generate_review_with_bedrock(changes_data: Any) -> List[Dict[str, Any]]:
    """Generate code review using Amazon Bedrock with proper JSON handling"""
    reviews = []

    try:
        # First parse the input if it's a string
        if isinstance(changes_data, str):
            try:
                parsed_data = json.loads(changes_data)
                changes = parsed_data.get('changes', [])
            except json.JSONDecodeError as e:
                logger.error("Failed to parse input JSON", exc_info=True)
                return []
        elif isinstance(changes_data, dict):
            changes = changes_data.get('changes', [])
        else:
            logger.error(f"Unexpected input type: {type(changes_data)}")
            return []

        if not isinstance(changes, list):
            logger.error(f"Changes should be a list, got {type(changes)}")
            return []

        if not changes:
            logger.warning("No changes provided to generate review")
            return reviews

        logger.info(f"Generating reviews for {len(changes)} files")

        for index, change in enumerate(changes, start=1):
            try:
                if not isinstance(change, dict):
                    logger.warning(f"Skipping non-dictionary change at position {index}")
                    continue

                filename = change.get('filename', f'unknown_file_{index}')

                if 'patch' not in change or not change['patch']:
                    logger.warning(f"Skipping file {filename} as it has no patch content")
                    continue

                logger.debug(f"Processing file {index}/{len(changes)}: {filename}")

                prompt = f"""
                Review the following code changes and provide:
                1. A detailed review comment
                2. Code improvement suggestions
                3. Potential issues or bugs

                File: {filename}
                Status: {change.get('status', 'unknown')}
                Changes:
                {change['patch']}
                """

                logger.debug(f"Generated prompt for {filename}")

                # Prepare the Bedrock request
                request_body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": MAX_TOKENS,
                    "temperature": TEMPERATURE,
                    "messages": [{
                        "role": "user",
                        "content": prompt
                    }]
                }

                logger.debug(f"Sending request to Bedrock for {filename}")
                response = bedrock.invoke_model(
                    modelId=BEDROCK_MODEL_ID,
                    body=json.dumps(request_body))

                # Process the response
                response_body = json.loads(response['body'].read())

                if not response_body.get('content'):
                    raise PRReviewError("No content in Bedrock response")

                review_text = ""
                for content in response_body['content']:
                    if content['type'] == 'text':
                        review_text += content['text'] + "\n"

                if not review_text.strip():
                    raise PRReviewError("Empty review content from Bedrock")

                reviews.append({
                    'file': filename,
                    'review': review_text.strip(),
                    'status': change.get('status', 'reviewed'),
                    'model': BEDROCK_MODEL_ID
                })

                logger.debug(f"Successfully generated review for {filename}")

            except (BotoCoreError, ClientError) as be:
                error_msg = f"Bedrock API error for {filename}: {str(be)}"
                logger.error(error_msg)
                reviews.append({
                    'file': filename,
                    'error': error_msg,
                    'status': 'failed'
                })
            except Exception as e:
                error_msg = f"Unexpected error processing {filename}: {str(e)}"
                logger.error(error_msg, exc_info=True)
                reviews.append({
                    'file': filename,
                    'error': error_msg,
                    'status': 'failed'
                })

        success_count = len([r for r in reviews if 'error' not in r])
        logger.info(f"Completed review generation. Success: {success_count}, Failed: {len(changes) - success_count}")
        return reviews

    except Exception as e:
        logger.error(f"Critical error in review generation: {str(e)}", exc_info=True)
        return [{
            'error': f"Failed to process reviews: {str(e)}",
            'status': 'failed'
        }]


@tracer.capture_method()
def post_review_comments(repo_name: str, pr_number: int, owner: str, reviews_data: Any):
    """Post review comments to GitHub with robust JSON handling"""
    try:
        logger.info(f"Posting comments to PR {pr_number} in {owner}/{repo_name}")

        # Parse reviews_data if it's a string
        if isinstance(reviews_data, str):
            try:
                parsed_data = json.loads(reviews_data)
                reviews = parsed_data.get('reviews', []) if isinstance(parsed_data, dict) else []
            except json.JSONDecodeError as e:
                logger.error("Failed to parse reviews JSON", exc_info=True)
                raise PRReviewError("Invalid reviews format - could not parse JSON")
        elif isinstance(reviews_data, dict):
            reviews = reviews_data.get('reviews', [])
        elif isinstance(reviews_data, list):
            reviews = reviews_data
        else:
            logger.error(f"Unexpected reviews type: {type(reviews_data)}")
            raise PRReviewError(f"Invalid reviews format - expected list or dict, got {type(reviews_data)}")

        if not isinstance(reviews, list):
            logger.error(f"Reviews should be a list, got {type(reviews)}")
            raise PRReviewError("Invalid reviews format - expected list")

        logger.info(f"Processing {len(reviews)} reviews")
        logger.debug(f"Sample review: {reviews[0] if reviews else 'No reviews'}")

        repo = github.get_repo(f"{owner}/{repo_name}")
        pr = repo.get_pull(pr_number)

        successful_posts = 0
        failed_posts = 0

        for review in reviews:
            try:
                if not isinstance(review, dict):
                    logger.warning(f"Skipping non-dictionary review: {review}")
                    failed_posts += 1
                    continue

                if 'error' in review:
                    logger.warning(f"Skipping failed review: {review.get('file', 'unknown_file')}")
                    failed_posts += 1
                    continue

                filename = review.get('file', 'unknown_file')
                review_text = review.get('review', '')

                if not review_text:
                    logger.warning(f"Skipping empty reviews for {filename}")
                    failed_posts += 1
                    continue

                logger.debug(f"Posting review for {filename}")
                pr.create_review(
                    body=review_text,
                    event='COMMENT'
                )
                successful_posts += 1
                logger.debug(f"Successfully posted review for {filename}")

            except GithubException as ge:
                failed_posts += 1
                logger.error(f"GitHub API error posting review for {filename}: {str(ge)}")
            except Exception as e:
                failed_posts += 1
                logger.error(f"Unexpected error posting review for {filename}: {str(e)}", exc_info=True)

        logger.info(f"Review posting completed. Successful: {successful_posts}, Failed: {failed_posts}")
        return {
            'status': 'completed',
            'successful_posts': successful_posts,
            'failed_posts': failed_posts
        }

    except GithubException as ge:
        error_msg = f"GitHub API error: {str(ge)}"
        logger.error(error_msg)
        raise PRReviewError(error_msg)
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        logger.error(error_msg, exc_info=True)
        raise PRReviewError(error_msg)


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
@tracer.capture_lambda_handler
@metrics.log_metrics
def lambda_handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """Main Lambda handler with enhanced error handling and logging"""
    try:
        logger.info("Lambda function invoked", extra={"event": event})

        action = event.get('action')
        if not action:
            error_msg = "No action specified in event"
            logger.error(error_msg)
            return {
                'statusCode': 400,
                'body': json.dumps({'error': error_msg})
            }

        logger.debug(f"Processing action: {action}")

        if action == 'fetch_changes':
            required_fields = ['repository', 'pull_request_number', 'owner']
            if not all(field in event for field in required_fields):
                error_msg = f"Missing required fields for {action}. Required: {required_fields}"
                logger.error(error_msg)
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': error_msg})
                }

            changes = fetch_pr_changes(
                event['repository'],
                event['pull_request_number'],
                event['owner']
            )
            return {
                'statusCode': 200,
                'body': json.dumps({'changes': changes})
            }

        elif action == 'generate_review':
            if 'changes' not in event or not event['changes']:
                error_msg = "No changes provided for review generation"
                logger.error(error_msg)
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': error_msg})
                }

            reviews = generate_review_with_bedrock(event['changes'])
            return {
                'statusCode': 200,
                'body': json.dumps({'reviews': reviews})
            }

        elif action == 'post_comments':
            required_fields = ['repository', 'pull_request_number', 'owner', 'review']
            if not all(field in event for field in required_fields):
                error_msg = f"Missing required fields for {action}. Required: {required_fields}"
                logger.error(error_msg)
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': error_msg})
                }

            post_review_comments(
                event['repository'],
                event['pull_request_number'],
                event['owner'],
                event['review']
            )
            return {
                'statusCode': 200,
                'body': json.dumps({'status': 'success'})
            }

        else:
            error_msg = f"Unknown action: {action}"
            logger.error(error_msg)
            return {
                'statusCode': 400,
                'body': json.dumps({'error': error_msg})
            }

    except PRReviewError as pre:
        logger.error(f"PR Review Error: {str(pre)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(pre)})
        }
    except Exception as e:
        logger.error(f"Unexpected error in lambda handler: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': 'Internal server error'})
        }
