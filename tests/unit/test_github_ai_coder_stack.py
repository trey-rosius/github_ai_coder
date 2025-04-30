import aws_cdk as core
import aws_cdk.assertions as assertions

from github_ai_coder.github_ai_coder_stack import GithubAiCoderStack

# example tests. To run these tests, uncomment this file along with the example
# resource in github_ai_coder/github_ai_coder_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = GithubAiCoderStack(app, "github-ai-coder")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
