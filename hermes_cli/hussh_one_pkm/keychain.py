# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Minimal macOS Keychain adapter with no command-line secret exposure."""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_char_p, c_uint32, c_void_p


class KeychainError(RuntimeError):
    pass


class MacOSKeychain:
    _SUCCESS = 0
    _NOT_FOUND = -25300

    def __init__(self, *, service: str = "ai.hushh.one.hermes") -> None:
        if sys.platform != "darwin":
            raise KeychainError(
                "Hussh One trusted-device storage currently requires macOS."
            )
        self._service = service.encode("utf-8")
        self._security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        self._core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._configure()

    def _configure(self) -> None:
        self._security.SecKeychainFindGenericPassword.argtypes = [
            c_void_p,
            c_uint32,
            c_void_p,
            c_uint32,
            c_void_p,
            POINTER(c_uint32),
            POINTER(c_void_p),
            POINTER(c_void_p),
        ]
        self._security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainAddGenericPassword.argtypes = [
            c_void_p,
            c_uint32,
            c_void_p,
            c_uint32,
            c_void_p,
            c_uint32,
            c_void_p,
            POINTER(c_void_p),
        ]
        self._security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self._security.SecKeychainItemModifyAttributesAndData.argtypes = [
            c_void_p,
            c_void_p,
            c_uint32,
            c_void_p,
        ]
        self._security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self._security.SecKeychainItemDelete.argtypes = [c_void_p]
        self._security.SecKeychainItemDelete.restype = ctypes.c_int32
        self._security.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
        self._security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self._core.CFRelease.argtypes = [c_void_p]

    @staticmethod
    def _buffer(value: bytes) -> ctypes.Array:
        return ctypes.create_string_buffer(value, len(value))

    def _find(self, account: str) -> tuple[int, bytes | None, c_void_p]:
        account_bytes = account.encode("utf-8")
        service_buffer = self._buffer(self._service)
        account_buffer = self._buffer(account_bytes)
        length = c_uint32()
        data = c_void_p()
        item = c_void_p()
        status = self._security.SecKeychainFindGenericPassword(
            None,
            len(self._service),
            service_buffer,
            len(account_bytes),
            account_buffer,
            byref(length),
            byref(data),
            byref(item),
        )
        if status == self._SUCCESS:
            secret = ctypes.string_at(data, length.value)
            self._security.SecKeychainItemFreeContent(None, data)
            return status, secret, item
        return status, None, item

    def get(self, account: str) -> bytes | None:
        status, secret, item = self._find(account)
        if item:
            self._core.CFRelease(item)
        if status == self._NOT_FOUND:
            return None
        if status != self._SUCCESS:
            raise KeychainError(f"Keychain read failed with OSStatus {status}.")
        return secret

    def set(self, account: str, secret: bytes) -> None:
        status, _existing, item = self._find(account)
        secret_buffer = self._buffer(secret)
        if status == self._SUCCESS:
            try:
                update_status = self._security.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret), secret_buffer
                )
            finally:
                if item:
                    self._core.CFRelease(item)
            if update_status != self._SUCCESS:
                raise KeychainError(
                    f"Keychain update failed with OSStatus {update_status}."
                )
            return
        if status != self._NOT_FOUND:
            raise KeychainError(f"Keychain lookup failed with OSStatus {status}.")

        account_bytes = account.encode("utf-8")
        service_buffer = self._buffer(self._service)
        account_buffer = self._buffer(account_bytes)
        add_status = self._security.SecKeychainAddGenericPassword(
            None,
            len(self._service),
            service_buffer,
            len(account_bytes),
            account_buffer,
            len(secret),
            secret_buffer,
            None,
        )
        if add_status != self._SUCCESS:
            raise KeychainError(f"Keychain write failed with OSStatus {add_status}.")

    def delete(self, account: str) -> None:
        status, _secret, item = self._find(account)
        if status == self._NOT_FOUND:
            return
        if status != self._SUCCESS or not item:
            raise KeychainError(f"Keychain lookup failed with OSStatus {status}.")
        try:
            delete_status = self._security.SecKeychainItemDelete(item)
        finally:
            self._core.CFRelease(item)
        if delete_status != self._SUCCESS:
            raise KeychainError(
                f"Keychain delete failed with OSStatus {delete_status}."
            )
