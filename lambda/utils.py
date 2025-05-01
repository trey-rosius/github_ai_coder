import boto3
import json


def get_github_token() -> str:
    """
    Fetch Stripe secret key from AWS Secrets Manager.
    Adjust the SecretId and region name based on your setup.
    """
    secret_name = "dev/github_token"  # Replace with your actual secret name for Stripe
    region_name = "us-east-1"  # Replace with your secrets region

    # Create a session and Secrets Manager client
    session = boto3.session.Session()
    client = session.client(service_name="secretsmanager", region_name=region_name)

    try:
        # Retrieve the secret value
        response = client.get_secret_value(SecretId=secret_name)
        secret_string = response[
            "SecretString"
        ]  # e.g., '{"STRIPE_SECRET_KEY": "sk_test_123..."}'
        secret_dict = json.loads(secret_string)

        # Adjust the key used here to match your secret's JSON structure
        return secret_dict.get("GITHUB_TOKEN", "")
    except Exception as e:
        print(f"Error retrieving github token key: {e}")
        return ""
