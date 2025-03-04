# Copyright 2020 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
import warnings
from io import BytesIO

import pytest

import requests
from huggingface_hub.commands.user import _login
from huggingface_hub.constants import (
    REPO_TYPE_DATASET,
    REPO_TYPE_MODEL,
    REPO_TYPE_SPACE,
    SPACES_SDK_TYPES,
)
from huggingface_hub.file_download import cached_download, hf_hub_download
from huggingface_hub.hf_api import (
    USERNAME_PLACEHOLDER,
    DatasetInfo,
    DatasetSearchArguments,
    HfApi,
    HfFolder,
    MetricInfo,
    ModelInfo,
    ModelSearchArguments,
    _validate_repo_id_deprecation,
    erase_from_credential_store,
    read_from_credential_store,
    repo_type_and_id_from_hf_id,
)
from huggingface_hub.utils import logging
from huggingface_hub.utils.endpoint_helpers import (
    DatasetFilter,
    ModelFilter,
    _filter_emissions,
)
from requests.exceptions import HTTPError

from .testing_constants import (
    ENDPOINT_STAGING,
    ENDPOINT_STAGING_BASIC_AUTH,
    FULL_NAME,
    PASS,
    TOKEN,
    USER,
)
from .testing_utils import (
    DUMMY_DATASET_ID,
    DUMMY_DATASET_ID_REVISION_ONE_SPECIFIC_COMMIT,
    DUMMY_MODEL_ID,
    DUMMY_MODEL_ID_REVISION_ONE_SPECIFIC_COMMIT,
    require_git_lfs,
    retry_endpoint,
    set_write_permission_and_retry,
    with_production_testing,
)


logger = logging.get_logger(__name__)


def repo_name(id=uuid.uuid4().hex[:6]):
    return "my-model-{0}-{1}".format(id, int(time.time() * 10e3))


def repo_name_large_file(id=uuid.uuid4().hex[:6]):
    return "my-model-largefiles-{0}-{1}".format(id, int(time.time() * 10e3))


def dataset_repo_name(id=uuid.uuid4().hex[:6]):
    return "my-dataset-{0}-{1}".format(id, int(time.time() * 10e3))


def space_repo_name(id=uuid.uuid4().hex[:6]):
    return "my-space-{0}-{1}".format(id, int(time.time() * 10e3))


WORKING_REPO_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures/working_repo"
)
LARGE_FILE_14MB = "https://cdn-media.huggingface.co/lfs-largefiles/progit.epub"
LARGE_FILE_18MB = "https://cdn-media.huggingface.co/lfs-largefiles/progit.pdf"


class HfApiCommonTest(unittest.TestCase):
    _api = HfApi(endpoint=ENDPOINT_STAGING)


class HfApiLoginTest(HfApiCommonTest):
    def setUp(self) -> None:
        erase_from_credential_store(USER)

    @classmethod
    def tearDownClass(cls) -> None:
        with pytest.warns(FutureWarning, match="This method is deprecated"):
            cls._api.login(username=USER, password=PASS)

    def test_login_invalid(self):
        with pytest.warns(FutureWarning, match="This method is deprecated"):
            with pytest.raises(HTTPError):
                self._api.login(username=USER, password="fake")

    def test_login_valid(self):
        with pytest.warns(FutureWarning, match="This method is deprecated"):
            token = self._api.login(username=USER, password=PASS)
        assert isinstance(token, str)

    def test_login_git_credentials(self):
        self.assertTupleEqual(read_from_credential_store(USER), (None, None))
        with pytest.warns(FutureWarning, match="This method is deprecated"):
            self._api.login(username=USER, password=PASS)
        self.assertTupleEqual(read_from_credential_store(USER), (USER.lower(), PASS))
        erase_from_credential_store(username=USER)
        self.assertTupleEqual(read_from_credential_store(USER), (None, None))

    def test_login_cli(self):
        with pytest.warns(FutureWarning, match="This method is deprecated"):
            _login(self._api, username=USER, password=PASS)
        self.assertTupleEqual(read_from_credential_store(USER), (USER.lower(), PASS))
        erase_from_credential_store(username=USER)
        self.assertTupleEqual(read_from_credential_store(USER), (None, None))

        _login(self._api, token=TOKEN)
        self.assertTupleEqual(
            read_from_credential_store(USERNAME_PLACEHOLDER),
            (USERNAME_PLACEHOLDER, TOKEN),
        )
        erase_from_credential_store(username=USERNAME_PLACEHOLDER)
        self.assertTupleEqual(
            read_from_credential_store(USERNAME_PLACEHOLDER), (None, None)
        )

    def test_login_cli_org_fail(self):
        with pytest.raises(
            ValueError, match="You must use your personal account token."
        ):
            _login(self._api, token="api_org_dummy_token")

    def test_login_deprecation_error(self):
        with pytest.warns(
            FutureWarning,
            match=r"HfApi.login: This method is deprecated in favor of "
            r"`set_access_token` and will be removed in v0.7.",
        ):
            self._api.login(username=USER, password=PASS)

    def test_logout_deprecation_error(self):
        with pytest.warns(
            FutureWarning,
            match=r"HfApi.logout: This method is deprecated in favor of "
            r"`unset_access_token` and will be removed in v0.7.",
        ):
            try:
                self._api.logout()
            except HTTPError:
                pass


class HfApiCommonTestWithLogin(HfApiCommonTest):
    @classmethod
    def setUpClass(cls):
        """
        Share this valid token in all tests below.
        """
        cls._token = TOKEN
        cls._api.set_access_token(TOKEN)


@retry_endpoint
def test_repo_id_no_warning():
    # tests that passing repo_id as positional arg doesn't raise any warnings
    # for {create, delete}_repo and update_repo_visibility
    api = HfApi(endpoint=ENDPOINT_STAGING)
    REPO_NAME = repo_name("crud")

    args = [
        ("create_repo", {}),
        ("update_repo_visibility", {"private": False}),
        ("delete_repo", {}),
    ]

    for method, kwargs in args:
        with warnings.catch_warnings(record=True) as record:
            getattr(api, method)(
                REPO_NAME, token=TOKEN, repo_type=REPO_TYPE_MODEL, **kwargs
            )
        assert not len(record)


def test_validate_repo_id_deprecation():
    with pytest.warns(FutureWarning, match="input arguments are deprecated"):
        name, org = _validate_repo_id_deprecation(
            repo_id=None, name="repo", organization="org"
        )
        assert name == "repo" and org == "org"

    with warnings.catch_warnings(record=True) as record:
        name, org = _validate_repo_id_deprecation(
            repo_id="org/repo", name=None, organization=None
        )
        assert name == "repo" and org == "org"
    assert not len(record)

    with pytest.raises(ValueError, match="leave deprecated"):
        _validate_repo_id_deprecation(
            repo_id="repo_id", name="name", organization="organization"
        )

    # regression test for
    # https://github.com/huggingface/huggingface_hub/issues/821
    with pytest.warns(FutureWarning, match="input arguments are deprecated"):
        name, org = _validate_repo_id_deprecation(
            repo_id="repo", name=None, organization="org"
        )
        assert name == "repo" and org == "org"


@retry_endpoint
def test_name_org_deprecation_warning():
    # test that the right warning is raised when passing name to
    # {create, delete}_repo and update_repo_visibility
    api = HfApi(endpoint=ENDPOINT_STAGING)
    REPO_NAME = repo_name("crud")

    args = [
        ("create_repo", {}),
        ("update_repo_visibility", {"private": False}),
        ("delete_repo", {}),
    ]

    for method, kwargs in args:
        with pytest.warns(
            FutureWarning,
            match=re.escape("`name` and `organization` input arguments are deprecated"),
        ):
            getattr(api, method)(
                name=REPO_NAME, token=TOKEN, repo_type=REPO_TYPE_MODEL, **kwargs
            )


@retry_endpoint
def test_name_org_deprecation_error():
    # tests that the right error is raised when passing both name and repo_id
    # to {create, delete}_repo and update_repo_visibility
    api = HfApi(endpoint=ENDPOINT_STAGING)
    REPO_NAME = repo_name("crud")

    args = [
        ("create_repo", {}),
        ("update_repo_visibility", {"private": False}),
        ("delete_repo", {}),
    ]

    for method, kwargs in args:
        with pytest.raises(
            ValueError,
            match=re.escape("Only pass `repo_id`"),
        ):
            getattr(api, method)(
                repo_id="test",
                name=REPO_NAME,
                token=TOKEN,
                repo_type=REPO_TYPE_MODEL,
                **kwargs,
            )

    for method, kwargs in args:
        with pytest.raises(ValueError, match="No name provided"):
            getattr(api, method)(token=TOKEN, repo_type=REPO_TYPE_MODEL, **kwargs)


class HfApiEndpointsTest(HfApiCommonTestWithLogin):
    def test_whoami(self):
        info = self._api.whoami(token=self._token)
        self.assertEqual(info["name"], USER)
        self.assertEqual(info["fullname"], FULL_NAME)
        self.assertIsInstance(info["orgs"], list)
        valid_org = [org for org in info["orgs"] if org["name"] == "valid_org"][0]
        self.assertIsInstance(valid_org["apiToken"], str)

    @retry_endpoint
    def test_delete_repo_error_message(self):
        # test for #751
        with pytest.raises(
            HTTPError,
            match=(
                "No model repo found matching __DUMMY_TRANSFORMERS_USER__/"
                "repo-that-does-not-exist"
            ),
        ):
            self._api.delete_repo("repo-that-does-not-exist", token=self._token)

    @retry_endpoint
    def test_create_update_and_delete_repo(self):
        REPO_NAME = repo_name("crud")
        self._api.create_repo(repo_id=REPO_NAME, token=self._token)
        res = self._api.update_repo_visibility(
            repo_id=REPO_NAME, token=self._token, private=True
        )
        self.assertTrue(res["private"])
        res = self._api.update_repo_visibility(
            repo_id=REPO_NAME, token=self._token, private=False
        )
        self.assertFalse(res["private"])
        self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_create_update_and_delete_model_repo(self):
        REPO_NAME = repo_name("crud")
        self._api.create_repo(
            repo_id=REPO_NAME, token=self._token, repo_type=REPO_TYPE_MODEL
        )
        res = self._api.update_repo_visibility(
            repo_id=REPO_NAME,
            token=self._token,
            private=True,
            repo_type=REPO_TYPE_MODEL,
        )
        self.assertTrue(res["private"])
        res = self._api.update_repo_visibility(
            repo_id=REPO_NAME,
            token=self._token,
            private=False,
            repo_type=REPO_TYPE_MODEL,
        )
        self.assertFalse(res["private"])
        self._api.delete_repo(
            repo_id=REPO_NAME, token=self._token, repo_type=REPO_TYPE_MODEL
        )

    @retry_endpoint
    def test_create_update_and_delete_dataset_repo(self):
        DATASET_REPO_NAME = dataset_repo_name("crud")
        self._api.create_repo(
            repo_id=DATASET_REPO_NAME, token=self._token, repo_type=REPO_TYPE_DATASET
        )
        res = self._api.update_repo_visibility(
            repo_id=DATASET_REPO_NAME,
            token=self._token,
            private=True,
            repo_type=REPO_TYPE_DATASET,
        )
        self.assertTrue(res["private"])
        res = self._api.update_repo_visibility(
            repo_id=DATASET_REPO_NAME,
            token=self._token,
            private=False,
            repo_type=REPO_TYPE_DATASET,
        )
        self.assertFalse(res["private"])
        self._api.delete_repo(
            repo_id=DATASET_REPO_NAME, token=self._token, repo_type=REPO_TYPE_DATASET
        )

    @retry_endpoint
    def test_create_update_and_delete_space_repo(self):
        SPACE_REPO_NAME = space_repo_name("failing")
        with pytest.raises(ValueError, match=r"No space_sdk provided.*"):
            self._api.create_repo(
                token=self._token,
                repo_id=SPACE_REPO_NAME,
                repo_type=REPO_TYPE_SPACE,
                space_sdk=None,
            )
        with pytest.raises(ValueError, match=r"Invalid space_sdk.*"):
            self._api.create_repo(
                token=self._token,
                repo_id=SPACE_REPO_NAME,
                repo_type=REPO_TYPE_SPACE,
                space_sdk="asdfasdf",
            )

        for sdk in SPACES_SDK_TYPES:
            SPACE_REPO_NAME = space_repo_name(sdk)
            self._api.create_repo(
                repo_id=SPACE_REPO_NAME,
                token=self._token,
                repo_type=REPO_TYPE_SPACE,
                space_sdk=sdk,
            )
            res = self._api.update_repo_visibility(
                repo_id=SPACE_REPO_NAME,
                token=self._token,
                private=True,
                repo_type=REPO_TYPE_SPACE,
            )
            self.assertTrue(res["private"])
            res = self._api.update_repo_visibility(
                repo_id=SPACE_REPO_NAME,
                token=self._token,
                private=False,
                repo_type=REPO_TYPE_SPACE,
            )
            self.assertFalse(res["private"])
            self._api.delete_repo(
                repo_id=SPACE_REPO_NAME, token=self._token, repo_type=REPO_TYPE_SPACE
            )

    @retry_endpoint
    def test_move_repo(self):
        REPO_NAME = repo_name("crud")
        repo_id = f"__DUMMY_TRANSFORMERS_USER__/{REPO_NAME}"
        NEW_REPO_NAME = repo_name("crud2")
        new_repo_id = f"__DUMMY_TRANSFORMERS_USER__/{NEW_REPO_NAME}"

        for repo_type in [REPO_TYPE_MODEL, REPO_TYPE_DATASET, REPO_TYPE_SPACE]:
            self._api.create_repo(
                repo_id=REPO_NAME,
                token=self._token,
                repo_type=repo_type,
                space_sdk="static",
            )

            with pytest.raises(ValueError, match=r"Invalid repo_id*"):
                self._api.move_repo(
                    from_id=repo_id,
                    to_id="invalid_repo_id",
                    token=self._token,
                    repo_type=repo_type,
                )

            # Should raise an error if it fails
            self._api.move_repo(
                from_id=repo_id,
                to_id=new_repo_id,
                token=self._token,
                repo_type=repo_type,
            )
            self._api.delete_repo(
                repo_id=NEW_REPO_NAME, token=self._token, repo_type=repo_type
            )


class HfApiUploadFileTest(HfApiCommonTestWithLogin):
    def setUp(self) -> None:
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_file = os.path.join(self.tmp_dir, "temp")
        self.tmp_file_content = "Content of the file"
        with open(self.tmp_file, "w+") as f:
            f.write(self.tmp_file_content)
        self.addCleanup(
            lambda: shutil.rmtree(self.tmp_dir, onerror=set_write_permission_and_retry)
        )

    @retry_endpoint
    def test_upload_file_validation(self):
        REPO_NAME = repo_name("upload")
        with self.assertRaises(ValueError, msg="Wrong repo type"):
            self._api.upload_file(
                path_or_fileobj=self.tmp_file,
                path_in_repo="README.md",
                repo_id=f"{USER}/{REPO_NAME}",
                repo_type="this type does not exist",
                token=self._token,
            )

        with self.assertRaises(ValueError, msg="File opened in text mode"):
            with open(self.tmp_file, "rt") as ftext:
                self._api.upload_file(
                    path_or_fileobj=ftext,
                    path_in_repo="README.md",
                    repo_id=f"{USER}/{REPO_NAME}",
                    token=self._token,
                )

        with self.assertRaises(
            ValueError, msg="path_or_fileobj is str but does not point to a file"
        ):
            self._api.upload_file(
                path_or_fileobj=os.path.join(self.tmp_dir, "nofile.pth"),
                path_in_repo="README.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )

    @retry_endpoint
    def test_upload_file_path(self):
        REPO_NAME = repo_name("path")
        self._api.create_repo(token=self._token, repo_id=REPO_NAME)
        try:
            self._api.upload_file(
                path_or_fileobj=self.tmp_file,
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )
            url = "{}/{user}/{repo}/resolve/main/temp/new_file.md".format(
                ENDPOINT_STAGING,
                user=USER,
                repo=REPO_NAME,
            )
            filepath = cached_download(url, force_download=True)
            with open(filepath) as downloaded_file:
                content = downloaded_file.read()
            self.assertEqual(content, self.tmp_file_content)

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_upload_file_fileobj(self):
        REPO_NAME = repo_name("fileobj")
        self._api.create_repo(repo_id=REPO_NAME, token=self._token)
        try:
            with open(self.tmp_file, "rb") as filestream:
                self._api.upload_file(
                    path_or_fileobj=filestream,
                    path_in_repo="temp/new_file.md",
                    repo_id=f"{USER}/{REPO_NAME}",
                    token=self._token,
                )
            url = "{}/{user}/{repo}/resolve/main/temp/new_file.md".format(
                ENDPOINT_STAGING,
                user=USER,
                repo=REPO_NAME,
            )
            filepath = cached_download(url, force_download=True)
            with open(filepath) as downloaded_file:
                content = downloaded_file.read()
            self.assertEqual(content, self.tmp_file_content)

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_upload_file_bytesio(self):
        REPO_NAME = repo_name("bytesio")
        self._api.create_repo(repo_id=REPO_NAME, token=self._token)
        try:
            filecontent = BytesIO(b"File content, but in bytes IO")
            self._api.upload_file(
                path_or_fileobj=filecontent,
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )
            url = "{}/{user}/{repo}/resolve/main/temp/new_file.md".format(
                ENDPOINT_STAGING,
                user=USER,
                repo=REPO_NAME,
            )
            filepath = cached_download(url, force_download=True)
            with open(filepath) as downloaded_file:
                content = downloaded_file.read()
            self.assertEqual(content, filecontent.getvalue().decode())

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_create_repo_org_token_fail(self):
        REPO_NAME = repo_name("org")
        with pytest.raises(
            ValueError, match="You must use your personal account token."
        ):
            self._api.create_repo(repo_id=REPO_NAME, token="api_org_dummy_token")

    @retry_endpoint
    def test_create_repo_org_token_none_fail(self):
        REPO_NAME = repo_name("org")
        HfFolder.save_token("api_org_dummy_token")
        with pytest.raises(
            ValueError, match="You must use your personal account token."
        ):
            self._api.create_repo(repo_id=REPO_NAME)

    @retry_endpoint
    def test_upload_file_conflict(self):
        REPO_NAME = repo_name("conflict")
        self._api.create_repo(repo_id=REPO_NAME, token=self._token)
        try:
            filecontent = BytesIO(b"File content, but in bytes IO")
            self._api.upload_file(
                path_or_fileobj=filecontent,
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
                identical_ok=True,
            )

            # No exception raised when identical_ok is True
            self._api.upload_file(
                path_or_fileobj=filecontent,
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
                identical_ok=True,
            )

            with self.assertRaises(HTTPError) as err_ctx:
                self._api.upload_file(
                    path_or_fileobj=filecontent,
                    path_in_repo="temp/new_file.md",
                    repo_id=f"{USER}/{REPO_NAME}",
                    token=self._token,
                    identical_ok=False,
                )
                self.assertEqual(err_ctx.exception.response.status_code, 409)

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_upload_buffer(self):
        REPO_NAME = repo_name("buffer")
        self._api.create_repo(repo_id=REPO_NAME, token=self._token)
        try:
            buffer = BytesIO()
            buffer.write(self.tmp_file_content.encode())
            self._api.upload_file(
                path_or_fileobj=buffer.getvalue(),
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )
            url = "{}/{user}/{repo}/resolve/main/temp/new_file.md".format(
                ENDPOINT_STAGING,
                user=USER,
                repo=REPO_NAME,
            )
            filepath = cached_download(url, force_download=True)
            with open(filepath) as downloaded_file:
                content = downloaded_file.read()
            self.assertEqual(content, self.tmp_file_content)

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    @retry_endpoint
    def test_delete_file(self):
        REPO_NAME = repo_name("delete")
        self._api.create_repo(token=self._token, repo_id=REPO_NAME)
        try:
            self._api.upload_file(
                path_or_fileobj=self.tmp_file,
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )
            self._api.delete_file(
                path_in_repo="temp/new_file.md",
                repo_id=f"{USER}/{REPO_NAME}",
                token=self._token,
            )

            with self.assertRaises(HTTPError):
                # Should raise a 404
                hf_hub_download(f"{USER}/{REPO_NAME}", "temp/new_file.md")

        except Exception as err:
            self.fail(err)
        finally:
            self._api.delete_repo(repo_id=REPO_NAME, token=self._token)

    def test_get_full_repo_name(self):
        repo_name_with_no_org = self._api.get_full_repo_name("model", token=self._token)
        self.assertEqual(repo_name_with_no_org, f"{USER}/model")

        repo_name_with_no_org = self._api.get_full_repo_name(
            "model", organization="org", token=self._token
        )
        self.assertEqual(repo_name_with_no_org, "org/model")


class HfApiPublicTest(unittest.TestCase):
    def test_staging_list_models(self):
        _api = HfApi(endpoint=ENDPOINT_STAGING)
        _ = _api.list_models()

    @with_production_testing
    def test_list_models(self):
        _api = HfApi()
        models = _api.list_models()
        self.assertGreater(len(models), 100)
        self.assertIsInstance(models[0], ModelInfo)

    @with_production_testing
    def test_list_models_author(self):
        _api = HfApi()
        models = _api.list_models(author="google")
        self.assertGreater(len(models), 10)
        self.assertIsInstance(models[0], ModelInfo)
        [self.assertTrue("google" in model.author for model in models)]

    @with_production_testing
    def test_list_models_search(self):
        _api = HfApi()
        models = _api.list_models(search="bert")
        self.assertGreater(len(models), 10)
        self.assertIsInstance(models[0], ModelInfo)
        [self.assertTrue("bert" in model.modelId.lower()) for model in models]

    @with_production_testing
    def test_list_models_complex_query(self):
        # Let's list the 10 most recent models
        # with tags "bert" and "jax",
        # ordered by last modified date.
        _api = HfApi()
        models = _api.list_models(
            filter=("bert", "jax"), sort="lastModified", direction=-1, limit=10
        )
        # we have at least 1 models
        self.assertGreater(len(models), 1)
        self.assertLessEqual(len(models), 10)
        model = models[0]
        self.assertIsInstance(model, ModelInfo)
        self.assertTrue(all(tag in model.tags for tag in ["bert", "jax"]))

    @with_production_testing
    def test_list_models_with_config(self):
        _api = HfApi()
        models = _api.list_models(
            filter="adapter-transformers", fetch_config=True, limit=20
        )
        found_configs = 0
        for model in models:
            if model.config:
                found_configs = found_configs + 1
        self.assertGreater(found_configs, 0)

    @with_production_testing
    def test_model_info(self):
        _api = HfApi()
        model = _api.model_info(repo_id=DUMMY_MODEL_ID)
        self.assertIsInstance(model, ModelInfo)
        self.assertNotEqual(model.sha, DUMMY_MODEL_ID_REVISION_ONE_SPECIFIC_COMMIT)
        # One particular commit (not the top of `main`)
        model = _api.model_info(
            repo_id=DUMMY_MODEL_ID, revision=DUMMY_MODEL_ID_REVISION_ONE_SPECIFIC_COMMIT
        )
        self.assertIsInstance(model, ModelInfo)
        self.assertEqual(model.sha, DUMMY_MODEL_ID_REVISION_ONE_SPECIFIC_COMMIT)

    @with_production_testing
    def test_model_info_with_security(self):
        _api = HfApi()
        model = _api.model_info(
            repo_id=DUMMY_MODEL_ID,
            revision=DUMMY_MODEL_ID_REVISION_ONE_SPECIFIC_COMMIT,
            securityStatus=True,
        )
        self.assertEqual(
            getattr(model, "securityStatus"),
            {"containsInfected": False, "infectionTypes": []},
        )

    @with_production_testing
    def test_list_repo_files(self):
        _api = HfApi()
        files = _api.list_repo_files(repo_id=DUMMY_MODEL_ID)
        expected_files = [
            ".gitattributes",
            "README.md",
            "config.json",
            "flax_model.msgpack",
            "merges.txt",
            "pytorch_model.bin",
            "tf_model.h5",
            "vocab.json",
        ]
        self.assertListEqual(files, expected_files)

    def test_staging_list_datasets(self):
        _api = HfApi(endpoint=ENDPOINT_STAGING)
        _ = _api.list_datasets()

    @with_production_testing
    def test_list_datasets(self):
        _api = HfApi()
        datasets = _api.list_datasets()
        self.assertGreater(len(datasets), 100)
        self.assertIsInstance(datasets[0], DatasetInfo)

    @with_production_testing
    def test_filter_datasets_by_author_and_name(self):
        _api = HfApi()
        f = DatasetFilter(author="huggingface", dataset_name="DataMeasurementsFiles")
        datasets = _api.list_datasets(filter=f)
        self.assertEqual(len(datasets), 1)
        self.assertTrue("huggingface" in datasets[0].author)
        self.assertTrue("DataMeasurementsFiles" in datasets[0].id)

    @with_production_testing
    def test_filter_datasets_by_benchmark(self):
        _api = HfApi()
        f = DatasetFilter(benchmark="raft")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("benchmark:raft" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_language_creator(self):
        _api = HfApi()
        f = DatasetFilter(language_creators="crowdsourced")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("language_creators:crowdsourced" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_language(self):
        _api = HfApi()
        f = DatasetFilter(languages="en")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("languages:en" in datasets[0].tags)
        args = DatasetSearchArguments()
        f = DatasetFilter(languages=(args.languages.en, args.languages.fr))
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("languages:en" in datasets[0].tags)
        self.assertTrue("languages:fr" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_multilinguality(self):
        _api = HfApi()
        f = DatasetFilter(multilinguality="yes")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("multilinguality:yes" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_size_categories(self):
        _api = HfApi()
        f = DatasetFilter(size_categories="100K<n<1M")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("size_categories:100K<n<1M" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_task_categories(self):
        _api = HfApi()
        f = DatasetFilter(task_categories="audio-classification")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("task_categories:audio-classification" in datasets[0].tags)

    @with_production_testing
    def test_filter_datasets_by_task_ids(self):
        _api = HfApi()
        f = DatasetFilter(task_ids="automatic-speech-recognition")
        datasets = _api.list_datasets(filter=f)
        self.assertGreater(len(datasets), 0)
        self.assertTrue("task_ids:automatic-speech-recognition" in datasets[0].tags)

    @with_production_testing
    def test_list_datasets_full(self):
        _api = HfApi()
        datasets = _api.list_datasets(full=True)
        self.assertGreater(len(datasets), 100)
        dataset = datasets[0]
        self.assertIsInstance(dataset, DatasetInfo)
        self.assertTrue(any(dataset.cardData for dataset in datasets))

    @with_production_testing
    def test_list_datasets_author(self):
        _api = HfApi()
        datasets = _api.list_datasets(author="huggingface")
        self.assertGreater(len(datasets), 1)
        self.assertIsInstance(datasets[0], DatasetInfo)

    @with_production_testing
    def test_list_datasets_search(self):
        _api = HfApi()
        datasets = _api.list_datasets(search="wikipedia")
        self.assertGreater(len(datasets), 10)
        self.assertIsInstance(datasets[0], DatasetInfo)

    @with_production_testing
    def test_filter_datasets_with_cardData(self):
        _api = HfApi()
        datasets = _api.list_datasets(cardData=True)
        self.assertGreater(
            sum(
                [getattr(dataset, "cardData", None) is not None for dataset in datasets]
            ),
            0,
        )
        datasets = _api.list_datasets()
        self.assertTrue(
            all([getattr(dataset, "cardData", None) is None for dataset in datasets])
        )

    @with_production_testing
    def test_dataset_info(self):
        _api = HfApi()
        dataset = _api.dataset_info(repo_id=DUMMY_DATASET_ID)
        self.assertTrue(
            isinstance(dataset.cardData, dict) and len(dataset.cardData) > 0
        )
        self.assertTrue(
            isinstance(dataset.siblings, list) and len(dataset.siblings) > 0
        )
        self.assertIsInstance(dataset, DatasetInfo)
        self.assertNotEqual(dataset.sha, DUMMY_DATASET_ID_REVISION_ONE_SPECIFIC_COMMIT)
        dataset = _api.dataset_info(
            repo_id=DUMMY_DATASET_ID,
            revision=DUMMY_DATASET_ID_REVISION_ONE_SPECIFIC_COMMIT,
        )
        self.assertIsInstance(dataset, DatasetInfo)
        self.assertEqual(dataset.sha, DUMMY_DATASET_ID_REVISION_ONE_SPECIFIC_COMMIT)

    def test_staging_list_metrics(self):
        _api = HfApi(endpoint=ENDPOINT_STAGING)
        _ = _api.list_metrics()

    @with_production_testing
    def test_list_metrics(self):
        _api = HfApi()
        metrics = _api.list_metrics()
        self.assertGreater(len(metrics), 10)
        self.assertIsInstance(metrics[0], MetricInfo)
        self.assertTrue(any(metric.description for metric in metrics))

    @with_production_testing
    def test_filter_models_by_author(self):
        _api = HfApi()
        f = ModelFilter(author="muellerzr")
        models = _api.list_models(filter=f)
        self.assertGreater(len(models), 0)
        self.assertTrue("muellerzr" in models[0].modelId)

    @with_production_testing
    def test_filter_models_by_author_and_name(self):
        # Test we can search by an author and a name, but the model is not found
        _api = HfApi()
        f = ModelFilter("facebook", model_name="bart-base")
        models = _api.list_models(filter=f)
        self.assertTrue("facebook/bart-base" in models[0].modelId)

    @with_production_testing
    def test_failing_filter_models_by_author_and_model_name(self):
        # Test we can search by an author and a name, but the model is not found
        _api = HfApi()
        f = ModelFilter(author="muellerzr", model_name="testme")
        models = _api.list_models(filter=f)
        self.assertEqual(len(models), 0)

    @with_production_testing
    def test_filter_models_with_library(self):
        _api = HfApi()
        f = ModelFilter("microsoft", model_name="wavlm-base-sd", library="tensorflow")
        models = _api.list_models(filter=f)
        self.assertGreater(1, len(models))
        f = ModelFilter("microsoft", model_name="wavlm-base-sd", library="pytorch")
        models = _api.list_models(filter=f)
        self.assertGreater(len(models), 0)

    @with_production_testing
    def test_filter_models_with_task(self):
        _api = HfApi()
        f = ModelFilter(task="fill-mask", model_name="albert-base-v2")
        models = _api.list_models(filter=f)
        self.assertTrue("fill-mask" == models[0].pipeline_tag)
        self.assertTrue("albert-base-v2" in models[0].modelId)
        f = ModelFilter(task="dummytask")
        models = _api.list_models(filter=f)
        self.assertGreater(1, len(models))

    @with_production_testing
    def test_filter_models_by_language(self):
        _api = HfApi()
        f_fr = ModelFilter(language="fr")
        res_fr = _api.list_models(filter=f_fr)

        f_en = ModelFilter(language="en")
        res_en = _api.list_models(filter=f_en)

        assert len(res_fr) != len(res_en)

    @with_production_testing
    def test_filter_models_with_complex_query(self):
        _api = HfApi()
        args = ModelSearchArguments()
        f = ModelFilter(
            task=args.pipeline_tag.TextClassification,
            library=[args.library.PyTorch, args.library.TensorFlow],
        )
        models = _api.list_models(filter=f)
        self.assertGreater(len(models), 1)
        self.assertTrue(
            [
                "text-classification" in model.pipeline_tag
                or "text-classification" in model.tags
                for model in models
            ]
        )
        self.assertTrue(
            ["pytorch" in model.tags and "tf" in model.tags for model in models]
        )

    @with_production_testing
    def test_filter_models_with_cardData(self):
        _api = HfApi()
        models = _api.list_models(filter="co2_eq_emissions", cardData=True)
        self.assertTrue([hasattr(model, "cardData") for model in models])
        models = _api.list_models(filter="co2_eq_emissions")
        self.assertTrue(all([not hasattr(model, "cardData") for model in models]))

    def test_filter_emissions_dict(self):
        # tests that dictionary is handled correctly as "emissions" and that
        # 17g is accepted and parsed correctly as a value
        # regression test for #753
        model = ModelInfo(cardData={"co2_eq_emissions": {"emissions": "17g"}})
        res = _filter_emissions([model], -1, 100)
        assert len(res) == 1

    @with_production_testing
    def test_filter_emissions_with_max(self):
        _api = HfApi()
        models = _api.list_models(emissions_thresholds=(None, 100), cardData=True)
        self.assertTrue(
            all(
                [
                    model.cardData["co2_eq_emissions"] <= 100
                    for model in models
                    if isinstance(model.cardData["co2_eq_emissions"], (float, int))
                ]
            )
        )

    @with_production_testing
    def test_filter_emissions_with_min(self):
        _api = HfApi()
        models = _api.list_models(emissions_thresholds=(5, None), cardData=True)
        self.assertTrue(
            all(
                [
                    model.cardData["co2_eq_emissions"] >= 5
                    for model in models
                    if isinstance(model.cardData["co2_eq_emissions"], (float, int))
                ]
            )
        )

    @with_production_testing
    def test_filter_emissions_with_min_and_max(self):
        _api = HfApi()
        models = _api.list_models(emissions_thresholds=(5, 100), cardData=True)
        self.assertTrue(
            all(
                [
                    model.cardData["co2_eq_emissions"] >= 5
                    for model in models
                    if isinstance(model.cardData["co2_eq_emissions"], (float, int))
                ]
            )
        )
        self.assertTrue(
            all(
                [
                    model.cardData["co2_eq_emissions"] <= 100
                    for model in models
                    if isinstance(model.cardData["co2_eq_emissions"], (float, int))
                ]
            )
        )


class HfApiPrivateTest(HfApiCommonTestWithLogin):
    @retry_endpoint
    def setUp(self) -> None:
        super().setUp()
        self.REPO_NAME = repo_name("private")
        self._api.create_repo(repo_id=self.REPO_NAME, token=self._token, private=True)

    def tearDown(self) -> None:
        self._api.delete_repo(repo_id=self.REPO_NAME, token=self._token)

    def test_model_info(self):
        shutil.rmtree(os.path.dirname(HfFolder.path_token))
        # Test we cannot access model info without a token
        with self.assertRaisesRegex(requests.exceptions.HTTPError, "404 Client Error"):
            _ = self._api.model_info(repo_id=f"{USER}/{self.REPO_NAME}")
        # Test we can access model info with a token
        model_info = self._api.model_info(
            repo_id=f"{USER}/{self.REPO_NAME}", token=self._token
        )
        self.assertIsInstance(model_info, ModelInfo)

    @with_production_testing
    def test_list_private_models(self):
        orig = len(self._api.list_datasets())
        new = len(self._api.list_datasets(use_auth_token=self._token))
        self.assertGreater(new, orig)

    @with_production_testing
    def test_list_private_datasets(self):
        orig = len(self._api.list_models())
        new = len(self._api.list_models(use_auth_token=self._token))
        self.assertGreater(new, orig)


class HfFolderTest(unittest.TestCase):
    def test_token_workflow(self):
        """
        Test the whole token save/get/delete workflow,
        with the desired behavior with respect to non-existent tokens.
        """
        token = "token-{}".format(int(time.time()))
        HfFolder.save_token(token)
        self.assertEqual(HfFolder.get_token(), token)
        HfFolder.delete_token()
        HfFolder.delete_token()
        # ^^ not an error, we test that the
        # second call does not fail.
        self.assertEqual(HfFolder.get_token(), None)
        # test TOKEN in env
        self.assertEqual(HfFolder.get_token(), None)
        with unittest.mock.patch.dict(os.environ, {"HUGGING_FACE_HUB_TOKEN": token}):
            self.assertEqual(HfFolder.get_token(), token)


@require_git_lfs
class HfLargefilesTest(HfApiCommonTest):
    @classmethod
    def setUpClass(cls):
        """
        Share this valid token in all tests below.
        """
        cls._token = TOKEN
        cls._api.set_access_token(TOKEN)

    def setUp(self):
        self.REPO_NAME_LARGE_FILE = repo_name_large_file()
        if os.path.exists(WORKING_REPO_DIR):
            shutil.rmtree(WORKING_REPO_DIR, onerror=set_write_permission_and_retry)
        logger.info(
            f"Does {WORKING_REPO_DIR} exist: {os.path.exists(WORKING_REPO_DIR)}"
        )

    def tearDown(self):
        self._api.delete_repo(repo_id=self.REPO_NAME_LARGE_FILE, token=self._token)

    def setup_local_clone(self, REMOTE_URL):
        REMOTE_URL_AUTH = REMOTE_URL.replace(
            ENDPOINT_STAGING, ENDPOINT_STAGING_BASIC_AUTH
        )
        subprocess.run(
            ["git", "clone", REMOTE_URL_AUTH, WORKING_REPO_DIR],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "lfs", "track", "*.pdf"], check=True, cwd=WORKING_REPO_DIR
        )
        subprocess.run(
            ["git", "lfs", "track", "*.epub"], check=True, cwd=WORKING_REPO_DIR
        )

    @retry_endpoint
    def test_end_to_end_thresh_6M(self):
        self._api._lfsmultipartthresh = 6 * 10**6
        REMOTE_URL = self._api.create_repo(
            repo_id=self.REPO_NAME_LARGE_FILE,
            token=self._token,
        )
        self._api._lfsmultipartthresh = None
        self.setup_local_clone(REMOTE_URL)

        subprocess.run(
            ["wget", LARGE_FILE_18MB],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_REPO_DIR,
        )
        subprocess.run(["git", "add", "*"], check=True, cwd=WORKING_REPO_DIR)
        subprocess.run(
            ["git", "commit", "-m", "commit message"], check=True, cwd=WORKING_REPO_DIR
        )

        # This will fail as we haven't set up our custom transfer agent yet.
        failed_process = subprocess.run(
            ["git", "push"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_REPO_DIR,
        )
        self.assertEqual(failed_process.returncode, 1)
        self.assertIn("cli lfs-enable-largefiles", failed_process.stderr.decode())
        # ^ Instructions on how to fix this are included in the error message.

        subprocess.run(
            ["huggingface-cli", "lfs-enable-largefiles", WORKING_REPO_DIR], check=True
        )

        start_time = time.time()
        subprocess.run(["git", "push"], check=True, cwd=WORKING_REPO_DIR)
        print("took", time.time() - start_time)

        # To be 100% sure, let's download the resolved file
        pdf_url = f"{REMOTE_URL}/resolve/main/progit.pdf"
        DEST_FILENAME = "uploaded.pdf"
        subprocess.run(
            ["wget", pdf_url, "-O", DEST_FILENAME],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_REPO_DIR,
        )
        dest_filesize = os.stat(os.path.join(WORKING_REPO_DIR, DEST_FILENAME)).st_size
        self.assertEqual(dest_filesize, 18685041)

    @retry_endpoint
    def test_end_to_end_thresh_16M(self):
        # Here we'll push one multipart and one non-multipart file in the same commit, and see what happens
        self._api._lfsmultipartthresh = 16 * 10**6
        REMOTE_URL = self._api.create_repo(
            repo_id=self.REPO_NAME_LARGE_FILE,
            token=self._token,
        )
        self._api._lfsmultipartthresh = None
        self.setup_local_clone(REMOTE_URL)

        subprocess.run(
            ["wget", LARGE_FILE_18MB],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_REPO_DIR,
        )
        subprocess.run(
            ["wget", LARGE_FILE_14MB],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=WORKING_REPO_DIR,
        )
        subprocess.run(["git", "add", "*"], check=True, cwd=WORKING_REPO_DIR)
        subprocess.run(
            ["git", "commit", "-m", "both files in same commit"],
            check=True,
            cwd=WORKING_REPO_DIR,
        )

        subprocess.run(
            ["huggingface-cli", "lfs-enable-largefiles", WORKING_REPO_DIR], check=True
        )

        start_time = time.time()
        subprocess.run(["git", "push"], check=True, cwd=WORKING_REPO_DIR)
        print("took", time.time() - start_time)


class HfApiMiscTest(unittest.TestCase):
    def test_repo_type_and_id_from_hf_id(self):
        possible_values = {
            "https://huggingface.co/id": [None, None, "id"],
            "https://huggingface.co/user/id": [None, "user", "id"],
            "https://huggingface.co/datasets/user/id": ["dataset", "user", "id"],
            "https://huggingface.co/spaces/user/id": ["space", "user", "id"],
            "user/id": [None, "user", "id"],
            "dataset/user/id": ["dataset", "user", "id"],
            "space/user/id": ["space", "user", "id"],
            "id": [None, None, "id"],
        }

        for key, value in possible_values.items():
            self.assertEqual(
                repo_type_and_id_from_hf_id(key, hub_url="https://huggingface.co"),
                tuple(value),
            )
