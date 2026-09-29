#!/usr/bin/env python3
"""Build the credential-free, self-contained MuseLab menu Shortcut."""

from __future__ import annotations
import argparse
import json
import plistlib
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

SERVER_PLACEHOLDER = "https://muselab.example"
TOKEN_PLACEHOLDER = "PASTE_MUSELAB_TOKEN_HERE"


def text(*parts: str | dict) -> dict:
    """Interpolate action outputs using Apple's UTF-16 NSRange coordinates."""
    value = ""
    attachments = {}
    for part in parts:
        if isinstance(part, str):
            value += part
        else:
            position = len(value.encode("utf-16-le")) // 2
            attachments[f"{{{position}, 1}}"] = part["Value"]
            value += "\ufffc"
    return {
        "Value": {"string": value, "attachmentsByRange": attachments},
        "WFSerializationType": "WFTextTokenString",
    }


def text_variable(ref: dict) -> dict:
    """Pin conditional inputs to Text so iOS selects the string parameter."""
    value = dict(ref["Value"])
    value["Aggrandizements"] = [
        *value.get("Aggrandizements", []),
        {"Type": "WFCoercionVariableAggrandizement", "CoercionItemClass": "WFStringContentItem"},
    ]
    return {**ref, "Value": value}


def dictionary(fields: dict) -> dict:
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFKey": text(key), "WFItemType": 0, "WFValue": text(value)}
                for key, value in fields.items()
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


class Workflow:
    def __init__(self, name: str):
        self.name = name
        self.actions: list[dict] = []

    def action(self, kind: str, output: str = "", **parameters) -> dict:
        identifier = str(
            uuid5(NAMESPACE_URL, f"muselab-watch:{self.name}:{len(self.actions)}")
        ).upper()
        parameters["UUID"] = identifier
        if output:
            parameters["CustomOutputName"] = output
        self.actions.append(
            {
                "WFWorkflowActionIdentifier": f"is.workflow.actions.{kind}",
                "WFWorkflowActionParameters": parameters,
            }
        )
        return {
            "Value": {
                "OutputUUID": identifier,
                "Type": "ActionOutput",
                "OutputName": output or kind,
            },
            "WFSerializationType": "WFTextTokenAttachment",
        }

    def alert(self, title: str, message: str | dict, cancel: bool = False) -> None:
        self.action(
            "alert",
            WFAlertActionTitle=title,
            WFAlertActionMessage=message,
            WFAlertActionCancelButtonShown=cancel,
        )

    def placeholder_guard(self, ref: dict, placeholder: str) -> None:
        group = str(uuid5(NAMESPACE_URL, f"guard:{self.name}:{placeholder}")).upper()
        self.action(
            "conditional",
            WFInput={"Type": "Variable", "Variable": text_variable(ref)},
            WFControlFlowMode=0,
            WFCondition=99,
            WFConditionalActionString=placeholder,
            GroupingIdentifier=group,
        )
        self.alert("请先配置 MuseLab", "在 iPhone 上编辑此快捷指令，填写顶部的服务器地址和令牌。")
        self.action("exit")
        self.action("conditional", WFControlFlowMode=2, GroupingIdentifier=group)

    def value(self, source: dict, key: str | dict, name: str) -> dict:
        return self.action(
            "getvalueforkey",
            name,
            WFInput=source,
            WFGetDictionaryValueType="Value",
            WFDictionaryKey=key if isinstance(key, str) else text(key),
        )

    def plist(self) -> dict:
        return {
            "WFWorkflowName": self.name,
            "WFWorkflowActions": self.actions,
            "WFWorkflowTypes": ["WatchKit"],
            "WFWorkflowClientVersion": "2302.0.4",
            "WFWorkflowClientRelease": "2302.0.4",
            "WFWorkflowMinimumClientVersion": 900,
            "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowInputContentItemClasses": [],
            "WFWorkflowOutputContentItemClasses": [],
            "WFWorkflowHasOutputFallback": False,
            "WFWorkflowImportQuestions": [],
            "WFWorkflowIcon": {
                "WFWorkflowIconGlyphNumber": 59511,
                "WFWorkflowIconStartColor": 4282601983,
            },
        }


def variable(name: str) -> dict:
    return {
        "Value": {"Type": "Variable", "VariableName": name},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def build_workflow() -> dict:
    w = Workflow("MuseLab")
    w.action(
        "comment",
        WFCommentActionText="MuseLab 统一菜单。只需填写下面的服务器地址和 token；客户端编号保持原值。",
    )
    server = w.action("gettext", "服务器地址", WFTextActionText=SERVER_PLACEHOLDER)
    token = w.action("gettext", "MuseLab token", WFTextActionText=TOKEN_PLACEHOLDER)
    client = w.action(
        "gettext", "客户端编号", WFTextActionText="8d03fdb7-305b-4f49-a53f-0e1c9608fd62"
    )
    w.placeholder_guard(server, SERVER_PLACEHOLDER)
    w.placeholder_guard(token, TOKEN_PLACEHOLDER)
    headers = dictionary({"X-Auth-Token": token, "Accept": "application/json"})

    def setvar(name, value):
        w.action("setvariable", WFVariableName=name, WFInput=value)

    def condition(value, match=None, empty=False, operator=4):
        group = str(uuid5(NAMESPACE_URL, f"console-condition:{len(w.actions)}")).upper()
        params = {
            "WFInput": {"Type": "Variable", "Variable": text_variable(value)},
            "WFControlFlowMode": 0,
            "WFCondition": 101 if empty else operator,
            "GroupingIdentifier": group,
        }
        if match is not None:
            params["WFConditionalActionString"] = match
        w.action("conditional", **params)
        return group

    def end(group):
        w.action("conditional", WFControlFlowMode=2, GroupingIdentifier=group)

    def parse_response(response, name):
        # URL Contents may be a File on iPhone/Watch. Extract its contents and
        # explicitly parse JSON before any Get Dictionary Value action.
        raw = w.action("detect.text", name + "返回文本", WFInput=response)
        matched = w.action(
            "text.match",
            name + "格式检查",
            text=text(raw),
            WFMatchTextPattern=r'(?s)^\s*\{(?=.*"protocol_version"\s*:\s*2\s*[,}]).*\}\s*$',
            WFMatchTextCaseSensitive=True,
        )
        match_length = w.action(
            "count",
            name + "格式检查字数",
            WFInput=text_variable(matched),
            Input=text_variable(matched),
            WFCountType="Characters",
        )
        invalid = condition(match_length, "0")
        w.alert("连接未成功", "MuseLab 返回的内容无法读取。请检查服务器地址和 token，并稍后重试。")
        w.action("exit")
        end(invalid)
        return w.action("detect.dictionary", name + "字典", WFInput=raw)

    response = w.action(
        "downloadurl",
        "主菜单",
        WFURL=text(server, "/api/watch/menu?client_id=", client),
        WFHTTPMethod="GET",
        WFHTTPHeaders=headers,
    )
    setvar("界面", parse_response(response, "主菜单"))
    loop = str(uuid5(NAMESPACE_URL, "muselab-console-loop")).upper()
    w.action("repeat.count", WFControlFlowMode=0, WFRepeatCount=40, GroupingIdentifier=loop)
    view = variable("界面")
    kind = w.value(view, "kind", "界面类型")
    guard = condition(kind, empty=True)
    w.alert("连接未成功", "未收到 MuseLab 菜单。请检查服务器地址和 token；服务器地址末尾不要加 /。")
    w.action("exit")
    end(guard)
    stop = condition(kind, "exit")
    w.action("exit")
    end(stop)
    prompt = w.value(view, "message", "提示")
    action = w.value(view, "action", "默认操作")
    request_id = w.value(view, "request_id", "请求编号")
    setvar("操作", action)
    empty = w.action("gettext", "空内容", WFTextActionText="")
    setvar("输入", empty)

    menu = condition(kind, "menu")
    detail = w.value(view, "detail", "显示内容")
    # Count actual text instead of asking whether an empty content item exists.
    detail_count = w.action(
        "count",
        "显示内容字数",
        WFInput=text_variable(detail),
        Input=text_variable(detail),
        WFCountType="Characters",
    )
    has_detail = condition(detail_count, "0")
    w.action("conditional", WFControlFlowMode=1, GroupingIdentifier=has_detail)
    w.alert("MuseLab", text(detail))
    end(has_detail)
    labels = w.value(view, "labels", "菜单列表")
    choices = w.value(view, "choices", "操作映射")
    selected = w.action(
        "choosefromlist",
        "所选菜单",
        WFInput=labels,
        WFChooseFromListActionPrompt=text(prompt),
        WFChooseFromListActionSelectMultiple=False,
        WFChooseFromListActionSelectAll=False,
    )
    # The native exported action uses `text`, not WFInput, for Match Text.
    matched = w.action(
        "text.match",
        "编号匹配",
        text=text(selected),
        WFMatchTextPattern="^[0-9]{2}",
        WFMatchTextCaseSensitive=True,
    )
    first = w.action("getitemfromlist", "首个编号", WFInput=matched, WFItemSpecifier="First Item")
    key = w.action("gettext", "文本编号", WFTextActionText=text(first))
    chosen = w.value(choices, key, "所选操作")
    setvar("操作", chosen)
    end(menu)

    input_group = condition(kind, "input")
    content = w.action(
        "ask",
        "输入内容",
        WFAskActionPrompt=text(prompt),
        WFInputType="Text",
        WFAskActionDefaultAnswer="",
    )
    setvar("输入", content)
    end(input_group)

    confirm = condition(kind, "confirm")
    w.alert("确认操作", text(prompt), cancel=True)
    end(confirm)
    message_group = condition(kind, "message")
    accepted = condition(prompt, "消息已接受并排队。", operator=8)
    w.alert("MuseLab", "消息发送成功，Muse 正在处理中。")
    w.action("conditional", WFControlFlowMode=1, GroupingIdentifier=accepted)
    w.alert("MuseLab", text(prompt))
    end(accepted)
    end(message_group)

    response = w.action(
        "downloadurl",
        "下一界面",
        WFURL=text(server, "/api/watch/actions"),
        WFHTTPMethod="POST",
        WFHTTPHeaders=headers,
        WFHTTPBodyType="JSON",
        WFJSONValues=dictionary(
            {
                "client_id": client,
                "request_id": request_id,
                "action": variable("操作"),
                "text": variable("输入"),
            }
        ),
    )
    setvar("界面", parse_response(response, "下一界面"))
    w.action("repeat.count", WFControlFlowMode=2, GroupingIdentifier=loop)
    w.alert("MuseLab", "本次菜单操作已结束，可以重新打开 MuseLab。")
    workflow = w.plist()
    workflow["WFWorkflowClientVersion"] = "5037.109"
    workflow.pop("WFWorkflowClientRelease", None)
    workflow["WFWorkflowTypes"] = ["Watch", "WFWorkflowTypeShowInSearch"]
    workflow["WFQuickActionSurfaces"] = []
    workflow["WFWorkflowHasShortcutInputVariables"] = False
    workflow["WFWorkflowImportQuestions"] = [
        {
            "ActionIndex": 1,
            "Category": "Parameter",
            "ParameterKey": "WFTextActionText",
            "Text": "MuseLab 的 HTTPS 地址是什么？末尾不要加 /。",
            "DefaultValue": SERVER_PLACEHOLDER,
        },
        {
            "ActionIndex": 2,
            "Category": "Parameter",
            "ParameterKey": "WFTextActionText",
            "Text": "请输入 MuseLab token。",
            "DefaultValue": "",
        },
    ]
    return workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    workflow = build_workflow()
    (args.output / "MuseLab.unsigned.shortcut").write_bytes(
        plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY, sort_keys=False)
    )
    (args.output / "MuseLab.xml.plist").write_bytes(plistlib.dumps(workflow, sort_keys=False))
    print(
        json.dumps(
            {"actions": len(workflow["WFWorkflowActions"]), "credentials": "placeholders only"}
        )
    )


if __name__ == "__main__":
    main()
