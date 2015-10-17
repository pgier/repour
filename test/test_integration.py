import io
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

try:
    import docker
    import requests
    import requests_oauthlib
    import oauthlib.oauth2
    deps_available=True
except ImportError:
    deps_available=False

# Only run integration tests if able and requested
run_integration_tests = deps_available and "REPOUR_RUN_IT" in os.environ

#
# Docker Utils
#

def wait_in_logs(client, container, target_text):
    log = client.logs(
        container=container,
        stream=True,

    )

    for raw_line in log:
        line = raw_line.decode("utf-8")
        if target_text in line:
            break
    else:
        raise Exception("Container exited before target text '{target_text}' was found".format(**locals()))

#
# Tests
#

if run_integration_tests:
    class TestGitLabIntegration(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            # current file is in test/ relative to repo root
            repo_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

            # Docker client
            cls.client = docker.Client(version="1.19")

            # Build main image
            repour_it_image = "repour_integration_test"
            # Using list to drain the log stream (don't care about it)
            list(cls.client.build(
                path=repo_root,
                rm=True,
                forcerm=True,
                tag=repour_it_image,
            ))

            # Create repour volume dir
            cls.config_dir = tempfile.TemporaryDirectory()

            # Create key pair
            rkp = os.path.join(cls.config_dir.name, "repour")
            subprocess.check_call(["ssh-keygen", "-f", rkp, "-N", ""])
            os.rename(rkp, os.path.join(cls.config_dir.name, "repour.key"))
            with open(os.path.join(cls.config_dir.name, "repour.pub"), "r") as f:
                repour_public_key = f.read().strip()

            # Download PME jar
            r = requests.get(
                url="http://central.maven.org/maven2/org/commonjava/maven/ext/pom-manipulation-cli/1.7/pom-manipulation-cli-1.7.jar",
                stream=True,
            )
            r.raise_for_status()
            with open(os.path.join(cls.config_dir.name, "pom-manipulation-cli.jar"), "wb") as f:
                shutil.copyfileobj(r.raw, f)

            # Inital repour config, to be updated once GitLab is configured
            repour_config = {
                "log": {
                    "path": "/home/repour/vol/server.log",
                    "level": "INFO",
                },
                "bind": {
                    "address": None,
                    "port": 7331,
                },
                "repo_provider": {
                    "type": "gitlab",
                    "params": {
                        "root_url": "http://gitlab:8282",
                        "ssh_root_url": "ssh://git@gitlab:2222",
                        "group": {
                            "id": None,
                            "name": "mw-build",
                        },
                        "username": "repour",
                        "password": "pa$$w0rd",
                    },
                },
                "adjust_provider": {
                    "type": "subprocess",
                    "params": {
                        "description": "PME",
                        "cmd": [
                            "java",
                            "-jar",
                            "/home/repour/vol/pom-manipulation-cli.jar",
                            "-s",
                            "settings.xml",
                            "-d",
                            "-DrestURL=http://10.19.208.25:8180/da/rest/v-0.2/reports/lookup/gav",
                            "-Dversion.incremental.suffix=redhat",
                            "-DstrictAlignment=true",
                        ],
                    },
                },
            }

            cls.containers = []
            try:
                # Pull/create/start GitLab
                cls.gitlab_port = 54421
                docker_image = "docker.io/gitlab/gitlab-ce:8.0.4-ce.1"
                cls.client.pull(docker_image)
                gitlab_container = cls.client.create_container(
                    image=docker_image,
                    detach=True,
                    host_config=cls.client.create_host_config(
                        port_bindings={
                            80: ("127.0.0.1", cls.gitlab_port)
                        },
                    ),
                )["Id"]
                cls.containers.append(gitlab_container)
                cls.client.start(gitlab_container)
                wait_in_logs(cls.client, gitlab_container, "master process ready")

                # Setup requests for GitLab OAuth
                cls.gitlab_url="http://localhost:{cls.gitlab_port}".format(**locals())
                cls.gitlab_api_url="{cls.gitlab_url}/api/v3".format(**locals())
                cls.gitlab = requests_oauthlib.OAuth2Session(
                    client=oauthlib.oauth2.LegacyApplicationClient(
                        client_id="",
                    ),
                )
                # Disable InsecureTransportError
                os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
                cls.gitlab.fetch_token(
                    token_url="{cls.gitlab_url}/oauth/token".format(**locals()),
                    username="root",
                    password="5iveL!fe",
                )

                # Configure GitLab
                # Create repour user
                r = cls.gitlab.post(
                    url=cls.gitlab_api_url + "/users",
                    data={
                        "email": "repour@localhost.local",
                        "password": repour_config["repo_provider"]["params"]["password"],
                        "username": repour_config["repo_provider"]["params"]["username"],
                        "name": "Repour",
                        "confirm": "false",
                    },
                )
                r.raise_for_status()
                cls.gitlab_repour_uid = r.json()["id"]

                # Add ssh key to repour user
                r = cls.gitlab.post(
                    url="{cls.gitlab_api_url}/users/{cls.gitlab_repour_uid}/keys".format(**locals()),
                    data={
                        "title": "main",
                        "key": repour_public_key,
                    }
                )
                r.raise_for_status()

                # Create group
                r = cls.gitlab.post(
                    url="{cls.gitlab_api_url}/groups".format(**locals()),
                    data={
                        "name": repour_config["repo_provider"]["params"]["group"]["name"],
                        "path": repour_config["repo_provider"]["params"]["group"]["name"],
                        "description": "Build sources",
                    }
                )
                r.raise_for_status()
                cls.gitlab_gid = r.json()["id"]
                repour_config["repo_provider"]["params"]["group"]["id"] = cls.gitlab_gid

                # Add repour user to group
                r = cls.gitlab.post(
                    url="{cls.gitlab_api_url}/groups/{cls.gitlab_gid}/members".format(**locals()),
                    data={
                        "user_id": cls.gitlab_repour_uid,
                        "access_level": "50",
                    }
                )
                r.raise_for_status()

                # Write repour config
                with open(os.path.join(cls.config_dir.name, "config.yaml"), "w") as f:
                    yaml.dump(repour_config, f)

                # Create/start Repour
                cls.repour_port = 54422
                cls.repour_api_url="http://localhost:{cls.repour_port}".format(**locals())
                repour_container = cls.client.create_container(
                    image=repour_it_image,
                    detach=True,
                    host_config=cls.client.create_host_config(
                        links={ gitlab_container: "gitlab" },
                        binds={
                            cls.config_dir.name: {
                                "bind": "/home/repour/vol",
                                "mode": "z",
                            }
                        },
                        port_bindings={
                            7331: ("127.0.0.1", cls.repour_port)
                        },
                    ),
                )["Id"]
                cls.containers.append(repour_container)
                cls.client.start(repour_container)
                wait_in_logs(cls.client, repour_container, "Server started on socket")
            except Exception:
                for container in cls.containers:
                    cls.client.remove_container(
                        container=container,
                        force=True,
                    )
                cls.config_dir.cleanup()
                raise

        @classmethod
        def tearDownClass(cls):
            for container in cls.containers:
                cls.client.remove_container(
                    container=container,
                    force=True,
                )
            cls.config_dir.cleanup()

        def test_pull_git_noref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "jboss-modules-1.5.0",
                    "type": "git",
                    "url": "https://github.com/jboss-modules/jboss-modules.git",
                },
            )
            r.raise_for_status()
            internal = r.json()
            # TODO apply validation schema
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("jboss-modules", internal["url"])

        def test_pull_git_ref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "jboss-modules-1.5.0",
                    "type": "git",
                    "ref": "1.5.0.Beta1",
                    "url": "https://github.com/jboss-modules/jboss-modules.git",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("jboss-modules", internal["url"])

        def test_pull_git_commitid(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "jboss-modules-1.5.0",
                    "type": "git",
                    "ref": "2d8307585e97fff3a86c34eb86c681ba81bb1811",
                    "url": "https://github.com/jboss-modules/jboss-modules.git",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("jboss-modules", internal["url"])

        def test_pull_hg_noref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "hello",
                    "type": "hg",
                    "url": "https://selenic.com/repo/hello",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("hello", internal["url"])

        def test_pull_hg_ref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "hello",
                    "type": "hg",
                    "ref": "default",
                    "url": "https://selenic.com/repo/hello",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("hello", internal["url"])

        def test_pull_svn_noref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "apache-commons-io",
                    "type": "svn",
                    "url": "https://svn.apache.org/viewvc/commons/proper/io/tags/commons-io-2.5",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("apache-commons-io", internal["url"])

        def test_pull_svn_ref(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "apache-commons-io",
                    "type": "svn",
                    "ref": "1709188",
                    "url": "https://svn.apache.org/viewvc/commons/proper/io/trunk",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("apache-commons-io", internal["url"])

        def test_pull_archive_targz(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "jboss-modules-1.5.0",
                    "type": "archive",
                    "url": "https://github.com/jboss-modules/jboss-modules/archive/1.4.4.Final.tar.gz",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("jboss-modules", internal["url"])

        def test_pull_archive_zip(self):
            r = self.gitlab.post(
                url=self.repour_api_url + "/pull",
                json={
                    "name": "jboss-modules-1.5.0",
                    "type": "archive",
                    "url": "https://github.com/jboss-modules/jboss-modules/archive/1.4.4.Final.zip",
                },
            )
            r.raise_for_status()
            internal = r.json()
            self.assertIn("pull", internal["branch"])
            self.assertIn("pull", internal["tag"])
            self.assertIn("jboss-modules", internal["url"])

        # TODO possibly use decorator on adjust tests to skip if PME restURL host isn't accessible