from app.services.mapping.user_message_content_projection import user_content_projection


def test_user_projection_excludes_generated_attachment_blocks_from_visible_text():
    content = [
        {"type": "text", "text": "请看这张图"},
        {
            "type": "text",
            "text": '<attachment path=".boxteam/sessions/s/attachments/a.png">',
            "metadata": {
                "origin": "generated",
                "kind": "attachment_manifest",
                "schema_version": 1,
                "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
                "path": ".boxteam/sessions/s/attachments/a.png",
            },
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/webp;base64,preview"},
            "metadata": {
                "origin": "generated",
                "kind": "attachment_preview",
                "schema_version": 1,
                "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
            },
        },
    ]

    projection = user_content_projection(
        content,
        {
            "attachments": [
                {
                    "file_id": "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_7f071235805fbf58a2524f77bff76e13",
                    "name": "a.png",
                    "content_type": "image/png",
                }
            ]
        },
    )

    assert projection.visible_text == "请看这张图"
    assert projection.attachments[0]["name"] == "a.png"
    assert projection.rich_blocks[0]["image_url"]["url"].endswith("preview")
    assert 'image_url' not in projection.visible_text


def test_user_projection_never_json_dumps_unknown_blocks_and_does_not_mutate_source():
    content = [
        {"type": "text", "text": "正文"},
        {"type": "provider_extension", "payload": {"nested": [1]}},
    ]

    projection = user_content_projection(content)

    assert projection.visible_text == "正文"
    assert projection.unknown_block_types == ("provider_extension",)
    assert projection.blocks[1]["payload"] == {"nested": [1]}
    projection.blocks[1]["payload"]["nested"].append(2)
    assert content[1]["payload"] == {"nested": [1]}
