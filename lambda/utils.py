import json
from typing import Dict, Any, List

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from github import Github
from github.GithubException import GithubException
from aws_lambda_powertools import Logger, Tracer

logger = Logger(service="pr-reviewer")
tracer = Tracer(service="pr-reviewer")

BEDROCK_MODEL_ID = 'anthropic.claude-3-5-sonnet-20240620-v1:0'
MAX_TOKENS = 1000
TEMPERATURE = 0.5
session = boto3.session.Session()
client = session.client(service_name="secretsmanager", region_name='us-east-1')
from typing import List, Optional
from pydantic import BaseModel, Field, ValidationError


# --- 1. File-level schema ---------------------------------------------------- #
class PullRequestFileChange(BaseModel):
    """
    Subset of the GitHub PR 'files' schema, mapped to PyGithub.PullRequestFile
    https://docs.github.com/en/rest/pulls/pulls#list-pull-requests-files
    """
    filename: str
    status: str  # e.g. "added", "modified", "removed", …
    additions: int
    deletions: int
    changes: int  # additions + deletions
    patch: Optional[str] = None  # unified diff; omitted on binaries

    # Extra metadata you may find useful later
    sha: Optional[str] = None
    blob_url: Optional[str] = Field(None, alias="blob_url")
    raw_url: Optional[str] = Field(None, alias="raw_url")
    contents_url: Optional[str] = None
    previous_filename: Optional[str] = None

    @classmethod
    def from_github_file(cls, gf: "PullRequestFile"):  # type: ignore
        """Convenience helper to build the model from a PyGithub object."""
        return cls(
            filename=gf.filename,
            status=gf.status,
            additions=gf.additions,
            deletions=gf.deletions,
            changes=gf.changes,
            patch=getattr(gf, "patch", None),
            sha=getattr(gf, "sha", None),
            blob_url=getattr(gf, "blob_url", None),
            raw_url=getattr(gf, "raw_url", None),
            contents_url=getattr(gf, "contents_url", None),
            previous_filename=getattr(gf, "previous_filename", None),
        )


class PRReviewError(Exception):
    """Custom exception for PR review errors  """


@tracer.capture_method()
def initialize_clients() -> tuple:
    """Create GitHub and Bedrock clients"""
    token = get_github_token()
    if not token:
        raise PRReviewError("No GitHub token")
    return Github(token), boto3.client('bedrock-runtime')


@tracer.capture_method()
def fetch_pr_changes(
        repo_name: str,
        pr_number: int,
        owner: str,
       as_dict: bool = False,
) -> List[PullRequestFileChange]:
    """
    Load a pull request and return a *validated* list of file-level changes.

    Raises
    ------
    PRReviewError
        If GitHub or data-validation errors occur.
    """
    github, _ = initialize_clients()

    # Grab the PR ------------------------------------------------------------- #
    try:
        repo = github.get_repo(f"{owner}/{repo_name}")
        pr = repo.get_pull(pr_number)
    except GithubException as e:
        raise PRReviewError(f"GitHub error: {e}") from e

    # Build & validate models ------------------------------------------------- #
    changes: List[PullRequestFileChange] = []
    for f in pr.get_files():
        try:
            model = PullRequestFileChange.from_github_file(f)
            changes.append(model.model_dump(by_alias=True) if as_dict else model)
        except ValidationError as ve:
            raise PRReviewError(
                f"Pydantic validation failed for file '{getattr(f, 'filename', 'unknown')}': {ve}"
            ) from ve

    return changes


@tracer.capture_method()
def generate_review_with_bedrock(changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Loop through each file-change dict, invoke Bedrock, and collect structured reviews.
    Returns a list of dicts with keys: file, review (string) OR error.
    """
    # Initialize Bedrock client once
    bedrock = boto3.client('bedrock-runtime')
    reviews: List[Dict[str, Any]] = []

    if not changes:
        logger.warning("generate_review_with_bedrock called with empty changes list")
        return reviews

    for idx, change in enumerate(changes, start=1):
        filename = change.get('filename', f'file_{idx}')
        patch = change.get('patch')
        if not patch:
            logger.warning(f"[{filename}] no patch content, skipping")
            reviews.append({
                'file': filename,
                'error': 'no patch content',
                'status': 'skipped'
            })
            continue

        # Build your prompt
        prompt = (
            f"Please review the following code diff and provide:\n"
            f" 1. A concise high-level summary.\n"
            f" 2. Detailed comments on potential bugs or logic errors.\n"
            f" 3. Suggestions for improvements or refactoring.\n\n"
            f"File: {filename}\n"
            f"Diff:\n{patch}\n"
        )

        logger.debug(f"[{filename}] sending to Bedrock; diff length={len(patch)} chars")
        try:
            response = bedrock.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": MAX_TOKENS,
                    "temperature": TEMPERATURE,
                    "messages": [{"role": "user", "content": prompt}]
                })
            )

            # Bedrock returns a streaming payload as bytes; read & parse
            raw_body = response['body'].read()
            payload = json.loads(raw_body)

            # Expecting payload like: { content: [ {type:"text", text:"..."}, ... ] }
            parts = payload.get('content', [])
            text = ""
            for part in parts:
                if part.get('type') == 'text' and part.get('text'):
                    text += part['text']

            if not text.strip():
                raise ValueError("Empty review text from Bedrock")

            reviews.append({
                'file': filename,
                'review': text.strip(),
                'status': 'succeeded',
                'model': BEDROCK_MODEL_ID
            })
            logger.info(f"[{filename}] review succeeded")

        except (BotoCoreError, ClientError) as aws_err:
            msg = f"Bedrock SDK error for {filename}: {aws_err}"
            logger.error(msg, exc_info=True)
            reviews.append({
                'file': filename,
                'error': msg,
                'status': 'failed'
            })
        except (json.JSONDecodeError, ValueError) as parse_err:
            msg = f"Invalid response format for {filename}: {parse_err}"
            logger.error(msg, exc_info=True)
            reviews.append({
                'file': filename,
                'error': msg,
                'status': 'failed'
            })
        except Exception as e:
            msg = f"Unexpected error for {filename}: {e}"
            logger.error(msg, exc_info=True)
            reviews.append({
                'file': filename,
                'error': msg,
                'status': 'failed'
            })

    # summary metrics
    success = sum(1 for r in reviews if r.get('status') == 'succeeded')
    fail = len(reviews) - success
    logger.info(f"Bedrock reviews done: {success} succeeded, {fail} failed")
    return reviews


@tracer.capture_method()
def post_review_comments(repo_name: str, pr_number: int, owner: str, reviews: Any) -> Dict[str, int]:
    """Iterate your reviews, post comments with PyGithub, tally successes/failures."""
    github, _ = initialize_clients()
    repo = github.get_repo(f"{owner}/{repo_name}")
    pr = repo.get_pull(pr_number)
    logger.info(f"reviews is {reviews}")
    parsed = json.loads(reviews)
    if isinstance(parsed, dict) and "reviews" in parsed:
        reviews_list = parsed["reviews"]
    elif isinstance(parsed, list):
        reviews_list = parsed
    else:
        logger.error(f"Unexpected structure for reviews: {parsed!r}")
        raise PRReviewError("Invalid reviews payload")
    success, fail = 0, 0
    for r in reviews_list:
        if 'error' in r or not r.get('review'):
            fail += 1
            continue
        pr.create_review(body=r['review'], event='COMMENT')
        success += 1
    return {'successful_posts': success, 'failed_posts': fail}


def get_github_token() -> str:
    """
    Fetch Stripe secret key from AWS Secrets Manager.
    Adjust the SecretId and region name based on your setup.
    """
    secret_name = "dev/github_token"  # Replace with your actual secret name for Github
    region_name = "us-east-1"  # Replace with your secrets region

    # Create a session and Secrets Manager client

    try:
        # Retrieve the secret value
        response = client.get_secret_value(SecretId=secret_name)
        secret_string = response[
            "SecretString"
        ]
        secret_dict = json.loads(secret_string)

        # Adjust the key used here to match your secret's JSON structure
        return secret_dict.get("GITHUB_TOKEN", "")
    except Exception as e:
        print(f"Error retrieving github token key: {e}")
        return ""

def get_slack_webhook() -> str:
    """
    Fetch Stripe secret key from AWS Secrets Manager.
    Adjust the SecretId and region name based on your setup.
    """
    secret_name = "dev/slack-webhook"  # Replace with your actual secret name for Github
    region_name = "us-east-1"  # Replace with your secrets region

    # Create a session and Secrets Manager client

    try:
        # Retrieve the secret value
        response = client.get_secret_value(SecretId=secret_name)
        secret_string = response[
            "SecretString"
        ]
        secret_dict = json.loads(secret_string)

        # Adjust the key used here to match your secret's JSON structure
        return secret_dict.get("SLACK_WEBHOOK", "")
    except Exception as e:
        print(f"Error retrieving slack webhook: {e}")
        return ""