from typing import List, Optional

import time
import click
import base64
import fnmatch
import json
import boto3
import subprocess
from dataclasses import dataclass

from mypy_boto3_ecr import ECRClient
from concurrent.futures import ThreadPoolExecutor
from mypy_boto3_ecr.type_defs import RepositoryTypeDef


@dataclass()
class Context:
    client: ECRClient
    registry_id: str


@dataclass()
class MirroredRepo:
    upstream_image: str
    repository_uri: str
    upstream_tags: List[str]


@click.group()
@click.option(
    "--registry-id", help="The registry ID. This is usually your AWS account ID."
)
@click.option("--role-arn", help="Assume a specific role to push to AWS")
@click.pass_context
def cli(ctx, registry_id, role_arn):
    client = boto3.client("ecr")
    # Assume a role, if required:
    if role_arn:
        click.echo("Assuming role...")
        sts_connection = boto3.client("sts")
        assume_role_object = sts_connection.assume_role(
            RoleArn=role_arn, RoleSessionName=f"ecr-mirror", DurationSeconds=3600
        )["Credentials"]

        tmp_access_key = assume_role_object["AccessKeyId"]
        tmp_secret_key = assume_role_object["SecretAccessKey"]
        security_token = assume_role_object["SessionToken"]
        client = boto3.client(
            "ecr",
            aws_access_key_id=tmp_access_key,
            aws_secret_access_key=tmp_secret_key,
            aws_session_token=security_token,
        )
    ctx.obj = Context(client=client, registry_id=registry_id)


@cli.command()
@click.pass_context
def sync(ctx):
    """
    Copy public images to ECR using ECR tags
    """
    repositories = find_repositories(ctx.obj.client, ctx.obj.registry_id)
    copy_repositories(ctx.obj.client, ctx.obj.registry_id, list(repositories))


@cli.command()
@click.argument("source")
@click.argument("destination-repository")
@click.pass_context
def copy(ctx, source, destination_repository):
    """
    Copy all tags that match a given glob expression into ECR
    """
    upstream_image, upstream_tag = source.split(":")
    repositories = [
        MirroredRepo(
            upstream_image=upstream_image,
            upstream_tags=[upstream_tag],
            repository_uri=destination_repository,
        )
    ]
    copy_repositories(ctx.obj.client, ctx.obj.registry_id, repositories)


@cli.command()
@click.pass_context
def list_repos(ctx):
    """
    List all repositories that will be synced
    """
    click.echo("Repositories to mirror:")
    for repo in find_repositories(ctx.obj.client, ctx.obj.registry_id):
        click.secho(f"- upstream: {repo.upstream_image}", fg="green")
        click.secho(f"  mirror: {repo.repository_uri}", fg="red")
        if repo.upstream_tags:
            click.secho(f"  tags: {repo.upstream_tags}", fg="yellow")


def ecr_login(client: ECRClient, registry_id: str) -> str:
    """
    Authenticate with ECR, returning a `username:password` pair
    """
    auth_response = client.get_authorization_token(registryIds=[registry_id])
    return base64.decodebytes(
        auth_response["authorizationData"][0]["authorizationToken"].encode()
    ).decode()


def copy_repositories(
    client: ECRClient, registry_id: str, repositories: List[MirroredRepo]
):
    """
    Perform the actual, concurrent copy of the images
    """
    token = ecr_login(client, registry_id)
    click.echo("Finding all tags to copy...")
    items = [
        (repo, tag)
        for repo in repositories
        for tag in find_tags_to_copy(repo.upstream_image, repo.upstream_tags)
    ]
    click.echo(f"Beginning the copy of {len(items)} images")

    with ThreadPoolExecutor(max_workers=4) as pool:
        # This code aint' beautiful, but whatever 🤷‍
        pool.map(
            lambda item: copy_image(
                f"{item[0].upstream_image}:{item[1]}",
                f"{item[0].repository_uri}:{item[1]}",
                token,
                sleep_time=1,
            ),
            items,
        )


def copy_image(source_image, dest_image, token, sleep_time):
    """
    Copy a single image using Skopeo
    """
    click.echo(
        f"Copying {click.style(source_image, fg='green')} to {click.style(dest_image, fg='blue')}"
    )
    args = [
        "skopeo",
        "--insecure-policy",
        "copy",
        f"docker://{source_image}",
        f"docker://{dest_image}",
        "--override-os=linux",
        "--override-arch=amd64",
    ]
    args_with_creds = args + [f"--dest-creds={token}"]
    try:
        subprocess.check_output(args_with_creds)
    except subprocess.CalledProcessError as e:
        click.secho(f'{" ".join(args)} raised an error: {e.returncode}', fg="red")
        click.secho(f"Last output: {e.output[100:]}", fg="red")

    time.sleep(sleep_time)


def find_tags_to_copy(image_name, tag_patterns):
    """
    Use Skopeo to list all available tags for an image
    """
    output = subprocess.check_output(
        ["skopeo", "list-tags", f"docker://{image_name}", "--override-os=linux"]
    )
    all_tags = json.loads(output)["Tags"]

    if not tag_patterns:
        return all_tags

    yield from (
        tag
        for tag in all_tags
        if any(fnmatch.fnmatch(tag, pattern) for pattern in tag_patterns)
    )


def find_repositories(client: ECRClient, registry_id: str):
    """
    List all ECR repositories that have an `upstream-image` tag set.
    """
    paginator = client.get_paginator("describe_repositories")
    all_repositories = [
        repo
        for result in paginator.paginate(registryId=registry_id)
        for repo in result["repositories"]
    ]

    def filter_repo(repo: RepositoryTypeDef) -> Optional[MirroredRepo]:
        tags = client.list_tags_for_resource(resourceArn=repo["repositoryArn"])
        tags_dict = {tag_item["Key"]: tag_item["Value"] for tag_item in tags["tags"]}

        if "upstream-image" in tags_dict:
            return MirroredRepo(
                upstream_image=tags_dict["upstream-image"],
                upstream_tags=tags_dict.get("upstream-tags", "")
                .replace("+", "*")
                .split("/"),
                repository_uri=repo["repositoryUri"],
            )

    with ThreadPoolExecutor() as pool:
        for item in pool.map(filter_repo, all_repositories):
            if item is not None:
                yield item
