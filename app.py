#!/usr/bin/env python3
import os

import aws_cdk as cdk

from github_ai_coder.github_ai_coder_stack import GithubAiCoderStack


app = cdk.App()
GithubAiCoderStack(app, "GithubAiCoderStack",
 )

app.synth()
