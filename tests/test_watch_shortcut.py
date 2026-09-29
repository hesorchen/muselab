"""Exercise exported Shortcuts actions with File/Text/Dictionary response items.

Native parameter keys are checked against ActionKit exports, independently of
our builder. This interpreter validates wiring, not iPhone/Watch device behavior.
"""

from dataclasses import dataclass
import json
import re

from scripts.build_watch_console import build_workflow

workflow = build_workflow()
actions = workflow["WFWorkflowActions"]


@dataclass(frozen=True)
class FileContent:
    data: bytes


def stringify(value):
    if value is None:
        return ""
    if isinstance(value, FileContent):
        return value.data.decode("utf-8")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "\n".join(stringify(item) for item in value)
    return str(value)


def wire(value, format):
    if format == "dictionary":
        return value
    text = stringify(value)
    return FileContent(text.encode("utf-8")) if format == "file" else text


actions = workflow["WFWorkflowActions"]
links = {}
stack = []
for i, a in enumerate(actions):
    p = a["WFWorkflowActionParameters"]
    if a["WFWorkflowActionIdentifier"].endswith(("conditional", "repeat.count")):
        mode = p["WFControlFlowMode"]
        gid = p["GroupingIdentifier"]
        if mode == 0:
            stack.append([gid, i, None])
        elif mode == 1:
            assert stack[-1][0] == gid
            stack[-1][2] = i
        elif mode == 2:
            group, start, otherwise = stack.pop()
            assert group == gid
            links[start] = (otherwise, i)
            links[i] = start
            if otherwise is not None:
                links[otherwise] = i
assert not stack


def view(kind="menu", message="MuseLab", detail="", labels=None):
    labels = labels or ["01 · ➕ 新建并发送", "02 · 🎙️ 继续上次会话", "03 · 🗂️ 最近会话"]
    return dict(
        protocol_version=2,
        kind=kind,
        message=message,
        detail=detail,
        labels=labels,
        choices={label[:2]: "fixture-action-" + label[:2] for label in labels},
        action="fixture-default-action",
        request_id="00000000-0000-4000-8000-000000000001",
    )


def run(views, wire_format="dictionary"):
    outputs = {}
    variables = {}
    ui = []
    posts = 0
    responses = iter(views)
    loop_counts = {}

    def resolve(item):
        if not isinstance(item, dict):
            return item
        if "Variable" in item and item.get("Type") == "Variable":
            return resolve(item["Variable"])
        kind = item.get("WFSerializationType")
        if kind == "WFTextTokenString":
            value = item["Value"]
            data = value["string"].encode("utf-16-le")
            for span, ref in sorted(
                value.get("attachmentsByRange", {}).items(),
                key=lambda pair: int(re.findall(r"\d+", pair[0])[0]),
                reverse=True,
            ):
                pos, length = map(int, re.findall(r"\d+", span))
                data = (
                    data[: pos * 2]
                    + str(
                        resolve({"WFSerializationType": "WFTextTokenAttachment", "Value": ref})
                    ).encode("utf-16-le")
                    + data[(pos + length) * 2 :]
                )
            return data.decode("utf-16-le")
        if kind == "WFDictionaryFieldValue":
            return {
                resolve(v["WFKey"]): resolve(v["WFValue"])
                for v in item["Value"]["WFDictionaryFieldValueItems"]
            }
        if kind == "WFTextTokenAttachment":
            value = item["Value"]
            if value["Type"] == "ActionOutput":
                result = outputs[value["OutputUUID"]]
            else:
                result = variables[value["VariableName"]]
            for change in value.get("Aggrandizements", []):
                if change.get("CoercionItemClass") == "WFStringContentItem":
                    result = stringify(result)
            return result
        raise AssertionError("unsupported serialized value")

    i = 0
    steps = 0
    while i < len(actions):
        steps += 1
        assert steps < 1000
        a = actions[i]
        kind = a["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")
        p = a["WFWorkflowActionParameters"]
        result = None
        if kind == "conditional":
            mode = p["WFControlFlowMode"]
            if mode == 0:
                value = resolve(p["WFInput"])
                comparison = p.get("WFConditionalActionString")
                operator = p["WFCondition"]
                passed = {
                    4: lambda: value == comparison,
                    8: lambda: str(value).startswith(comparison),
                    99: lambda: comparison in str(value),
                    101: lambda: value in ("", None),
                }[operator]()
                if not passed:
                    other, end = links[i]
                    i = other + 1 if other is not None else end + 1
                    continue
            elif mode == 1:
                i = links[i] + 1
                continue
        elif kind == "repeat.count":
            if p["WFControlFlowMode"] == 0:
                loop_counts[i] = p["WFRepeatCount"]
            else:
                start = links[i]
                loop_counts[start] -= 1
                if loop_counts[start]:
                    i = start + 1
                    continue
        elif kind == "gettext":
            if i == 1:
                result = "https://fixture.invalid"
            elif i == 2:
                result = "fixture-credential"
            else:
                result = resolve(p["WFTextActionText"])
        elif kind == "downloadurl":
            resolve(p["WFURL"])
            resolve(p["WFHTTPHeaders"])
            if p.get("WFHTTPMethod") == "POST":
                posts += 1
                resolve(p["WFJSONValues"])
            result = wire(next(responses, view("exit", "")), wire_format)
        elif kind == "detect.text":
            result = stringify(resolve(p["WFInput"]))
        elif kind == "detect.dictionary":
            result = json.loads(resolve(p["WFInput"]))
            assert isinstance(result, dict)
        elif kind == "setvariable":
            variables[p["WFVariableName"]] = resolve(p["WFInput"])
        elif kind == "getvalueforkey":
            source = resolve(p["WFInput"])
            assert isinstance(source, dict), "Dictionary Value received " + type(source).__name__
            result = source.get(resolve(p["WFDictionaryKey"]), "")
        elif kind == "count":
            assert p["WFCountType"] == "Characters"
            assert p["Input"] == p["WFInput"]
            result = len(resolve(p["Input"]))
        elif kind == "alert":
            ui.append(("alert", resolve(p["WFAlertActionMessage"])))
        elif kind == "choosefromlist":
            labels = resolve(p["WFInput"])
            ui.append(("menu", resolve(p["WFChooseFromListActionPrompt"])))
            result = labels[min(1, len(labels) - 1)]
        elif kind == "text.match":
            result = re.findall(p["WFMatchTextPattern"], resolve(p["text"]))
        elif kind == "getitemfromlist":
            assert p["WFItemSpecifier"] == "First Item"
            result = resolve(p["WFInput"])[0]
        elif kind == "ask":
            ui.append(("input", resolve(p["WFAskActionPrompt"])))
            result = "fixture message"
        elif kind == "exit":
            return ui, posts
        elif kind != "comment":
            raise AssertionError("unsupported action " + kind)
        outputs[p["UUID"]] = result
        i += 1
    return ui, posts


def test_watch_shortcut_native_parameters_and_import_questions():
    assert "Watch" in workflow["WFWorkflowTypes"]
    assert [question["ActionIndex"] for question in workflow["WFWorkflowImportQuestions"]] == [1, 2]
    assert actions[1]["WFWorkflowActionParameters"]["WFTextActionText"] == "https://muselab.example"
    assert (
        actions[2]["WFWorkflowActionParameters"]["WFTextActionText"] == "PASTE_MUSELAB_TOKEN_HERE"
    )
    identifiers = {action["WFWorkflowActionParameters"]["UUID"] for action in actions}

    def inspect(value):
        if isinstance(value, dict):
            if value.get("Type") == "ActionOutput":
                assert value["OutputUUID"] in identifiers
            assert value.get("PropertyName") != "Index"
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(workflow)
    for action in actions:
        kind = action["WFWorkflowActionIdentifier"]
        params = action["WFWorkflowActionParameters"]
        if kind in {"is.workflow.actions.detect.text", "is.workflow.actions.detect.dictionary"}:
            assert "WFInput" in params
        if kind == "is.workflow.actions.count":
            assert params["Input"] == params["WFInput"]
        if kind == "is.workflow.actions.text.match":
            assert "text" in params


def test_watch_shortcut_response_types_and_conversation_flows():
    checks = []
    for title in ["MuseLab", "最近会话", "会话空闲", "没有待执行消息"]:
        ui, posts = run([view(message=title)])
        assert ui == [("menu", title)] and posts == 1
        checks.append("blank-detail-" + title)
    for text in [
        "最近回复\nfixture reply",
        "你 · fixture\nhello\nMuseLab · fixture\nworld",
        "🧪 示例回复",
    ]:
        ui, posts = run([view(message="会话", detail=text)])
        assert ui == [("alert", text), ("menu", "会话")]
        checks.append("nonempty-content")
    ui, posts = run(
        [
            view(),
            view("input", "输入内容"),
            view("confirm", "确认发送？"),
            view(
                "message",
                "消息已接受并排队。\n稍后选择「查看最近回复」查看结果。\n需要授权时，请在 MuseLab 网页处理。",
            ),
            view(message="会话空闲", labels=["01 · 发送消息", "02 · 退出"]),
            view("exit", ""),
        ]
    )
    assert ui == [
        ("menu", "MuseLab"),
        ("input", "输入内容"),
        ("alert", "确认发送？"),
        ("alert", "消息发送成功，Muse 正在处理中。"),
        ("menu", "会话空闲"),
    ]
    assert posts == 5
    checks.append("full-new-send-short-ack-then-session")
    ui, posts = run(
        [
            view(
                message="同一会话",
                detail="最近回复 fixture one",
                labels=["01 · 刷新回复", "02 · 继续对话"],
            ),
            view("input", "输入下一条"),
            view("confirm", "确认第一条？"),
            view("message", "消息已接受并排队。"),
            view(message="执行中", labels=["01 · 发送消息", "02 · 查看最近回复"]),
            view(
                message="同一会话",
                detail="最近回复 fixture two",
                labels=["01 · 刷新回复", "02 · 继续对话"],
            ),
            view("input", "输入下一条"),
            view("confirm", "确认第二条？"),
            view("message", "消息已接受并排队。"),
            view(message="执行中", labels=["01 · 发送消息", "02 · 退出"]),
            view("exit", ""),
        ]
    )
    assert [value for kind, value in ui if kind == "alert"] == [
        "最近回复 fixture one",
        "确认第一条？",
        "消息发送成功，Muse 正在处理中。",
        "最近回复 fixture two",
        "确认第二条？",
        "消息发送成功，Muse 正在处理中。",
    ]
    assert sum(kind == "input" for kind, _ in ui) == 2 and posts == 10
    checks.append("reply-continue-two-turns")
    for message in [
        "内容为空，没有发送消息。",
        "操作未完成，请返回主菜单重试。",
        "消息已接受并排队，但无法保存上次会话。请从最近会话查看结果。",
    ]:
        ui, posts = run([view("message", message)])
        assert ui == [("alert", message)] and posts == 1
        checks.append("preserved-error-or-warning")
    ui, posts = run([view("exit", "")])
    assert ui == [] and posts == 0
    checks.append("exit-no-extra-ui")
    for wire_format in ["file", "text", "dictionary"]:
        ui, posts = run([view(message="首屏菜单")], wire_format)
        assert ui == [("menu", "首屏菜单")] and posts == 1
        checks.append("initial-response-" + wire_format)
        ui, posts = run(
            [
                view(),
                view(message="同一会话", detail="最近回复 · fixture response"),
                view("input", "继续对话"),
                view("confirm", "确认发送"),
                view("message", "消息已接受并排队。"),
                view(message="同一会话"),
                view("exit", ""),
            ],
            wire_format,
        )
        assert [message for kind, message in ui if kind == "alert"] == [
            "最近回复 · fixture response",
            "确认发送",
            "消息发送成功，Muse 正在处理中。",
        ]
        assert posts == 6
        checks.append("full-multi-turn-response-" + wire_format)
        for invalid in [
            "<html><body>Bad Gateway</body></html>",
            '{"detail":"Unauthorized"}',
            '{"kind":"menu","labels":[]}',
            "",
        ]:
            ui, posts = run([invalid], wire_format)
            assert (
                len(ui) == 1
                and ui[0][0] == "alert"
                and "返回的内容无法读取" in ui[0][1]
                and posts == 0
            )
            checks.append("invalid-initial-response-" + wire_format)
        ui, posts = run([view(), "<html><body>Bad Gateway</body></html>"], wire_format)
        assert (
            ui[0][0] == "menu"
            and ui[-1][0] == "alert"
            and "返回的内容无法读取" in ui[-1][1]
            and posts == 1
        )
        checks.append("invalid-followup-response-" + wire_format)

    assert len(checks) == 34
