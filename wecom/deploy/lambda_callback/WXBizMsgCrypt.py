#!/usr/bin/env python
# -*- encoding:utf-8 -*-
"""企业微信消息加解密 (Python3, 从 callback_python3 复制)"""
import logging
import base64
import random
import hashlib
import time
import struct
from Crypto.Cipher import AES
import xml.etree.cElementTree as ET
import socket

import ierror


class FormatException(Exception):
    pass


def throw_exception(message, exception_class=FormatException):
    raise exception_class(message)


class SHA1:
    def getSHA1(self, token, timestamp, nonce, encrypt):
        try:
            sortlist = [token, timestamp, nonce, encrypt]
            sortlist.sort()
            sha = hashlib.sha1()
            sha.update("".join(sortlist).encode())
            return ierror.WXBizMsgCrypt_OK, sha.hexdigest()
        except Exception as e:
            logging.getLogger().error(e)
            return ierror.WXBizMsgCrypt_ComputeSignature_Error, None


class XMLParse:
    AES_TEXT_RESPONSE_TEMPLATE = """<xml>
<Encrypt><![CDATA[%(msg_encrypt)s]]></Encrypt>
<MsgSignature><![CDATA[%(msg_signaturet)s]]></MsgSignature>
<TimeStamp>%(timestamp)s</TimeStamp>
<Nonce><![CDATA[%(nonce)s]]></Nonce>
</xml>"""

    def extract(self, xmltext):
        try:
            xml_tree = ET.fromstring(xmltext)
            encrypt = xml_tree.find("Encrypt")
            return ierror.WXBizMsgCrypt_OK, encrypt.text
        except Exception as e:
            logging.getLogger().error(e)
            return ierror.WXBizMsgCrypt_ParseXml_Error, None

    def generate(self, encrypt, signature, timestamp, nonce):
        resp_dict = {
            'msg_encrypt': encrypt,
            'msg_signaturet': signature,
            'timestamp': timestamp,
            'nonce': nonce,
        }
        return self.AES_TEXT_RESPONSE_TEMPLATE % resp_dict


class PKCS7Encoder:
    block_size = 32

    def encode(self, text):
        text_length = len(text)
        amount_to_pad = self.block_size - (text_length % self.block_size)
        if amount_to_pad == 0:
            amount_to_pad = self.block_size
        pad = chr(amount_to_pad)
        return text + (pad * amount_to_pad).encode()

    def decode(self, decrypted):
        pad = ord(decrypted[-1])
        if pad < 1 or pad > 32:
            pad = 0
        return decrypted[:-pad]


class Prpcrypt:
    def __init__(self, key):
        self.key = key
        self.mode = AES.MODE_CBC

    def encrypt(self, text, receiveid):
        text = text.encode()
        text = self.get_random_str() + struct.pack("I", socket.htonl(len(text))) + text + receiveid.encode()
        pkcs7 = PKCS7Encoder()
        text = pkcs7.encode(text)
        cryptor = AES.new(self.key, self.mode, self.key[:16])
        try:
            ciphertext = cryptor.encrypt(text)
            return ierror.WXBizMsgCrypt_OK, base64.b64encode(ciphertext)
        except Exception as e:
            logging.getLogger().error(e)
            return ierror.WXBizMsgCrypt_EncryptAES_Error, None

    def decrypt(self, text, receiveid):
        try:
            cryptor = AES.new(self.key, self.mode, self.key[:16])
            plain_text = cryptor.decrypt(base64.b64decode(text))
        except Exception as e:
            logging.getLogger().error(e)
            return ierror.WXBizMsgCrypt_DecryptAES_Error, None
        try:
            pad = plain_text[-1]
            content = plain_text[16:-pad]
            xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
            xml_content = content[4: xml_len + 4]
            from_receiveid = content[xml_len + 4:]
        except Exception as e:
            logging.getLogger().error(e)
            return ierror.WXBizMsgCrypt_IllegalBuffer, None
        if from_receiveid.decode('utf8') != receiveid:
            # 智能机器人回调的 receiveid 可能为空，跳过校验
            if from_receiveid.decode('utf8').strip():
                logging.getLogger().error(f"CorpID mismatch: expected={receiveid}, got={from_receiveid.decode('utf8')}")
                return ierror.WXBizMsgCrypt_ValidateCorpid_Error, None
        return 0, xml_content

    def get_random_str(self):
        return str(random.randint(1000000000000000, 9999999999999999)).encode()


class WXBizMsgCrypt:
    def __init__(self, sToken, sEncodingAESKey, sReceiveId):
        try:
            self.key = base64.b64decode(sEncodingAESKey + "=")
            assert len(self.key) == 32
        except:
            throw_exception("[error]: EncodingAESKey unvalid!", FormatException)
        self.m_sToken = sToken
        self.m_sReceiveId = sReceiveId

    def VerifyURL(self, sMsgSignature, sTimeStamp, sNonce, sEchoStr):
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(self.m_sToken, sTimeStamp, sNonce, sEchoStr)
        if ret != 0:
            return ret, None
        if not signature == sMsgSignature:
            return ierror.WXBizMsgCrypt_ValidateSignature_Error, None
        pc = Prpcrypt(self.key)
        ret, sReplyEchoStr = pc.decrypt(sEchoStr, self.m_sReceiveId)
        return ret, sReplyEchoStr

    def DecryptMsg(self, sPostData, sMsgSignature, sTimeStamp, sNonce):
        xmlParse = XMLParse()
        ret, encrypt = xmlParse.extract(sPostData)
        if ret != 0:
            return ret, None
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(self.m_sToken, sTimeStamp, sNonce, encrypt)
        if ret != 0:
            return ret, None
        if not signature == sMsgSignature:
            return ierror.WXBizMsgCrypt_ValidateSignature_Error, None
        pc = Prpcrypt(self.key)
        ret, xml_content = pc.decrypt(encrypt, self.m_sReceiveId)
        return ret, xml_content
