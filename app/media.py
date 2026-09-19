"""Persistable inbound media metadata and bounded WeChat CDN downloads."""
import io
import asyncio
import re
import hashlib
import secrets
import httpx
from PIL import Image
from wechatbot.crypto import decode_aes_key, decrypt_aes_ecb
from wechatbot.protocol import CDN_BASE_URL
from wechatbot.crypto import generate_aes_key, encrypt_aes_ecb, encode_aes_key_base64

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 10


def attachments_from_items(items):
    result = []
    for item in items:
        kind = item.get('type')
        if kind not in (2, 4):
            continue
        payload = item.get('image_item' if kind == 2 else 'file_item') or {}
        media = payload.get('media') or {}
        name = str(payload.get('file_name') or ('image' if kind == 2 else 'file.bin'))
        name = name.replace('\\', '/').split('/')[-1]
        name = re.sub(r'[\x00-\x1f<>:"|?*]', '_', name).strip(' .')[:180] or 'file.bin'
        result.append({'type':'image' if kind == 2 else 'file', 'filename':name,
            'query':media.get('encrypt_query_param'), 'key':payload.get('aeskey') or media.get('aes_key'),
            'status':'pending', 'file_id':None})
    return result


async def download_attachment(attachment):
    if not attachment.get('query') or not attachment.get('key'):
        raise ValueError('微信附件缺少下载参数或解密密钥，请重新发送')
    # Never follow a message-supplied URL or forward MA/WeChat credentials to the CDN.
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        async with client.stream('GET', CDN_BASE_URL+'/download', params={'encrypted_query_param':attachment['query']}) as r:
            if r.status_code != 200:
                raise ValueError(f'微信附件下载失败（HTTP {r.status_code}），请重新发送')
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_FILE_BYTES + 16:
                    raise ValueError('附件超过 10 MB，请缩小后重新发送')
                chunks.append(chunk)
    try:
        content = decrypt_aes_ecb(b''.join(chunks), decode_aes_key(attachment['key']))
    except Exception:
        raise ValueError('微信附件解密失败，请重新发送') from None
    if len(content) > MAX_FILE_BYTES:
        raise ValueError('附件超过 10 MB，请缩小后重新发送')
    if attachment['type'] == 'image':
        try:
            with Image.open(io.BytesIO(content)) as img:
                fmt = img.format
                img.verify()
            extension = {'JPEG':'jpg','PNG':'png','WEBP':'webp','GIF':'gif'}.get(fmt)
            if not extension:
                raise ValueError()
            attachment['filename'] = 'image.'+extension
        except Exception:
            raise ValueError('图片格式无法识别，请发送 JPG、PNG、WEBP 或 GIF 图片') from None
    return content


async def upload_wechat_file(wx, bot, content, filename):
    is_image = False
    try:
        with Image.open(io.BytesIO(content)) as img:
            is_image = img.format in ('JPEG','PNG','GIF','WEBP')
            img.verify()
    except Exception:
        is_image = False
    key=generate_aes_key()
    encrypted=encrypt_aes_ecb(content,key)
    filekey=secrets.token_hex(16)
    info=await wx.get_upload_url(bot['base_url'],bot['token'],filekey=filekey,media_type=1 if is_image else 3,
        to_user_id=bot['user_id'],rawsize=len(content),rawfilemd5=hashlib.md5(content).hexdigest(),
        filesize=len(encrypted),no_need_thumb=True,aeskey=key.hex())
    if not info.get('upload_param'):
        raise ValueError('微信未返回文件上传参数')
    url=wx.build_cdn_upload_url(CDN_BASE_URL,info['upload_param'],filekey)
    query=await asyncio.wait_for(wx.upload_to_cdn(url,encrypted),timeout=90)
    if is_image:
        return {'type':2,'image_item':{'mid_size':len(encrypted),
            'media':{'encrypt_query_param':query,'aes_key':encode_aes_key_base64(key),'encrypt_type':1}}}
    return {'type':4,'file_item':{'file_name':filename,'len':str(len(content)),
        'media':{'encrypt_query_param':query,'aes_key':encode_aes_key_base64(key),'encrypt_type':1}}}
