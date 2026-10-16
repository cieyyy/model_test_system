"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = ""
    protocol: str = "openai"
    description: str = ""
    enabled: bool = True
    api_key: str = ""  # 可选，提供则同时创建 Credential


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    protocol: str | None = None
    description: str | None = None
    enabled: bool | None = None


class CredentialCreate(BaseModel):
    label: str = "默认"
    api_key: str = Field(min_length=4)


class RevealRequest(BaseModel):
    password: str


class PasswordSetRequest(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class ModelCreate(BaseModel):
    provider_id: int
    model_name: str = Field(min_length=1, max_length=200)
    display_name: str = ""
    model_url: str = ""  # 官网/文档地址
    capabilities: dict = {}
    notes: str = ""


class ModelUpdate(BaseModel):
    provider_id: int | None = None
    display_name: str | None = None
    model_url: str | None = None
    capabilities: dict | None = None
    notes: str | None = None


class ModelFetchRequest(BaseModel):
    provider_id: int


class ModelsBulkCreate(BaseModel):
    provider_id: int
    model_names: list[str]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str = "通用"
    provider_id: int | None = None
    model_id: int | None = None
    script_path: str = ""
    command: str = ""
    script_dir: str = ""
    exec_mode: str = "builtin"  # builtin / script
    enabled: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    provider_id: int | None = None
    model_id: int | None = None
    script_path: str | None = None
    command: str | None = None
    script_dir: str | None = None
    exec_mode: str | None = None
    enabled: bool | None = None


class ImportScanRequest(BaseModel):
    root: str = ""  # 旧资料根目录，空则用默认


class ItemFilesRequest(BaseModel):
    root: str = ""
    name: str = ""  # 扫描项名称（根目录下的子目录/文件名）


class ImportExecuteRequest(BaseModel):
    root: str = ""
    folders: list[str] = []  # 要导入的子目录/文件名列表


class PreviewRequest(BaseModel):
    project_ids: list[int]


class ExecuteRequest(BaseModel):
    project_ids: list[int]
    confirmed: bool = False  # 真实付费请求必须显式确认为 True


class ScriptExecuteRequest(BaseModel):
    project_id: int
    script_names: list[str] | None = None  # 空=运行文件夹内全部可运行脚本
    command: str = ""  # 自定义命令（优先于 script_names；shell 执行，cwd=脚本文件夹）


class MatrixReviewRequest(BaseModel):
    provider_id: int
    model_name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=100)
    override_status: str = ""  # 空=清除覆盖；compatible/partial/unsupported/unknown/manual_review
    note: str = ""
