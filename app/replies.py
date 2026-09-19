"""Plain text formatting without joining distinct Agent messages."""
import re


def wechat_text(text):
    # Registered attachments are delivered separately; sandbox links cannot open in WeChat.
    text = re.sub(r'!?\[([^\]]*)\]\((?:sandbox:|file:|/mnt/)[^\n)]*\)', '', text)
    text = re.sub(r'!\[([^\]]*)\]\((https?://[^\s)]+)\)', r'\1：\2', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'\1（\2）', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.M)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def reply_chunks(text, limit=1500):
    text = wechat_text(text)
    chunks = []
    while len(text)>limit:
        end = max(text.rfind('\n\n',0,limit+1), text.rfind('\n',0,limit+1))
        if end < limit//3:
            end = max(text.rfind('。',0,limit),text.rfind('！',0,limit),text.rfind('？',0,limit))+1
        if end < limit//3:
            end = limit
        chunks.append(text[:end].strip())
        text = text[end:].lstrip()
    if text:
        chunks.append(text)
    return chunks
