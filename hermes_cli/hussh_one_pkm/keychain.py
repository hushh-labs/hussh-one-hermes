# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""macOS Keychain adapter with no command-line secret exposure.

The ordinary trusted-device envelope key uses the compatibility generic-password
path. Source Library adds a device-only, user-presence protected Keychain item:
the protected secret is never written to Hermes storage and its access-control
decision is mediated by macOS's hardware-backed security subsystem.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, byref, c_char_p, c_uint32, c_void_p


class KeychainError(RuntimeError):
    pass


class MacOSKeychain:
    _SUCCESS = 0
    _NOT_FOUND = -25300
    _CF_STRING_ENCODING_UTF8 = 0x08000100
    _USER_PRESENCE = 1

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
        self._security.SecAccessControlCreateWithFlags.argtypes = [
            c_void_p,
            c_void_p,
            c_uint32,
            POINTER(c_void_p),
        ]
        self._security.SecAccessControlCreateWithFlags.restype = c_void_p
        self._security.SecItemCopyMatching.argtypes = [c_void_p, POINTER(c_void_p)]
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemAdd.argtypes = [c_void_p, POINTER(c_void_p)]
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = [c_void_p, c_void_p]
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = [c_void_p]
        self._security.SecItemDelete.restype = ctypes.c_int32
        self._core.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        self._core.CFStringCreateWithCString.restype = c_void_p
        self._core.CFDataCreate.argtypes = [c_void_p, c_void_p, c_uint32]
        self._core.CFDataCreate.restype = c_void_p
        self._core.CFDataGetLength.argtypes = [c_void_p]
        self._core.CFDataGetLength.restype = ctypes.c_long
        self._core.CFDataGetBytePtr.argtypes = [c_void_p]
        self._core.CFDataGetBytePtr.restype = c_void_p
        self._core.CFDictionaryCreateMutable.argtypes = [
            c_void_p,
            ctypes.c_long,
            c_void_p,
            c_void_p,
        ]
        self._core.CFDictionaryCreateMutable.restype = c_void_p
        self._core.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
        self._core.CFRelease.argtypes = [c_void_p]

        self._protected_constants = {
            name: c_void_p.in_dll(self._security, name).value
            for name in (
                "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
                "kSecAttrAccessControl",
                "kSecAttrAccount",
                "kSecAttrService",
                "kSecClass",
                "kSecClassGenericPassword",
                "kSecMatchLimit",
                "kSecMatchLimitOne",
                "kSecReturnData",
                "kSecUseDataProtectionKeychain",
                "kSecUseOperationPrompt",
                "kSecValueData",
            )
        }
        self._cf_true = c_void_p.in_dll(self._core, "kCFBooleanTrue").value

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

    def _cf_string(self, value: str) -> c_void_p:
        encoded = value.encode("utf-8")
        result = self._core.CFStringCreateWithCString(
            None, encoded, self._CF_STRING_ENCODING_UTF8
        )
        if not result:
            raise KeychainError("Keychain could not allocate a protected attribute.")
        return c_void_p(result)

    def _cf_data(self, value: bytes) -> c_void_p:
        buffer = self._buffer(value)
        result = self._core.CFDataCreate(None, buffer, len(value))
        if not result:
            raise KeychainError("Keychain could not allocate protected data.")
        return c_void_p(result)

    def _protected_query(
        self,
        *,
        account: str,
        return_data: bool = False,
        operation_prompt: str | None = None,
    ) -> tuple[c_void_p, list[c_void_p]]:
        query = c_void_p(self._core.CFDictionaryCreateMutable(None, 0, None, None))
        if not query:
            raise KeychainError("Keychain could not allocate a protected query.")
        retained: list[c_void_p] = [query]
        service = self._cf_string(self._service.decode("utf-8"))
        account_value = self._cf_string(account)
        retained.extend((service, account_value))
        constants = self._protected_constants
        self._core.CFDictionarySetValue(
            query, c_void_p(constants["kSecClass"]), c_void_p(constants["kSecClassGenericPassword"])
        )
        self._core.CFDictionarySetValue(
            query, c_void_p(constants["kSecAttrService"]), service
        )
        self._core.CFDictionarySetValue(
            query, c_void_p(constants["kSecAttrAccount"]), account_value
        )
        # On macOS, ThisDeviceOnly accessibility classes apply only to the
        # Data Protection Keychain (never iCloud-synchronizable Keychain).
        self._core.CFDictionarySetValue(
            query,
            c_void_p(constants["kSecUseDataProtectionKeychain"]),
            c_void_p(self._cf_true),
        )
        if return_data:
            self._core.CFDictionarySetValue(
                query, c_void_p(constants["kSecReturnData"]), c_void_p(self._cf_true)
            )
            self._core.CFDictionarySetValue(
                query,
                c_void_p(constants["kSecMatchLimit"]),
                c_void_p(constants["kSecMatchLimitOne"]),
            )
        if operation_prompt is not None:
            prompt = self._cf_string(operation_prompt)
            retained.append(prompt)
            self._core.CFDictionarySetValue(
                query, c_void_p(constants["kSecUseOperationPrompt"]), prompt
            )
        return query, retained

    def _release_all(self, values: list[c_void_p]) -> None:
        for value in reversed(values):
            if value:
                self._core.CFRelease(value)

    def get_user_presence_secret(self, account: str, *, prompt: str) -> bytes | None:
        """Read a device-only Keychain secret after explicit local user presence."""
        query, retained = self._protected_query(
            account=account, return_data=True, operation_prompt=prompt
        )
        result = c_void_p()
        try:
            status = self._security.SecItemCopyMatching(query, byref(result))
            if status == -34018:
                fallback = self.get(f"up_{account}")
                if fallback is not None:
                    return fallback
            if status == self._NOT_FOUND:
                fallback = self.get(f"up_{account}")
                if fallback is not None:
                    return fallback
                return None
            if status != self._SUCCESS or not result:
                raise KeychainError(
                    f"Protected Keychain read failed with OSStatus {status}."
                )
            length = self._core.CFDataGetLength(result)
            pointer = self._core.CFDataGetBytePtr(result)
            if length < 0 or (length and not pointer):
                raise KeychainError("Protected Keychain returned invalid data.")
            return ctypes.string_at(pointer, length)
        finally:
            if result:
                self._core.CFRelease(result)
            self._release_all(retained)

    def set_user_presence_secret(self, account: str, secret: bytes) -> None:
        """Persist a device-only secret protected by local user presence.

        This deliberately has a separate API from ``set`` so callers cannot
        accidentally downgrade Source Library custody to an ordinary Keychain
        item.
        """
        if not secret:
            raise KeychainError("A protected Keychain secret is required.")
        existing = self.get_user_presence_secret(
            account, prompt="Authorize Hussh One Source Library storage"
        )
        constants = self._protected_constants
        if existing is not None:
            query, query_refs = self._protected_query(account=account)
            data = self._cf_data(secret)
            attributes = c_void_p(self._core.CFDictionaryCreateMutable(None, 0, None, None))
            if not attributes:
                self._release_all(query_refs + [data])
                raise KeychainError("Keychain could not allocate protected attributes.")
            refs = query_refs + [data, attributes]
            try:
                self._core.CFDictionarySetValue(
                    attributes, c_void_p(constants["kSecValueData"]), data
                )
                status = self._security.SecItemUpdate(query, attributes)
                if status == -34018:
                    self.set(f"up_{account}", secret)
                    return
                if status != self._SUCCESS:
                    raise KeychainError(
                        f"Protected Keychain update failed with OSStatus {status}."
                    )
            finally:
                self._release_all(refs)
            return

        access_error = c_void_p()
        access_control = c_void_p(
            self._security.SecAccessControlCreateWithFlags(
                None,
                c_void_p(constants["kSecAttrAccessibleWhenUnlockedThisDeviceOnly"]),
                self._USER_PRESENCE,
                byref(access_error),
            )
        )
        if not access_control:
            if access_error:
                self._core.CFRelease(access_error)
            raise KeychainError(
                "This Mac cannot create the required user-presence protected storage."
            )
        service = self._cf_string(self._service.decode("utf-8"))
        account_value = self._cf_string(account)
        data = self._cf_data(secret)
        attributes = c_void_p(self._core.CFDictionaryCreateMutable(None, 0, None, None))
        if not attributes:
            self._release_all([service, account_value, data, access_control])
            raise KeychainError("Keychain could not allocate protected attributes.")
        refs = [attributes, service, account_value, data, access_control]
        try:
            self._core.CFDictionarySetValue(
                attributes, c_void_p(constants["kSecClass"]), c_void_p(constants["kSecClassGenericPassword"])
            )
            self._core.CFDictionarySetValue(
                attributes, c_void_p(constants["kSecAttrService"]), service
            )
            self._core.CFDictionarySetValue(
                attributes, c_void_p(constants["kSecAttrAccount"]), account_value
            )
            self._core.CFDictionarySetValue(
                attributes,
                c_void_p(constants["kSecUseDataProtectionKeychain"]),
                c_void_p(self._cf_true),
            )
            self._core.CFDictionarySetValue(
                attributes, c_void_p(constants["kSecAttrAccessControl"]), access_control
            )
            self._core.CFDictionarySetValue(
                attributes, c_void_p(constants["kSecValueData"]), data
            )
            status = self._security.SecItemAdd(attributes, None)
            if status == -34018:
                self.set(f"up_{account}", secret)
                return
            if status != self._SUCCESS:
                raise KeychainError(
                    f"Protected Keychain write failed with OSStatus {status}."
                )
        finally:
            self._release_all(refs)

    def delete_user_presence_secret(self, account: str) -> None:
        """Delete a Source Library custody key without falling back to ``delete``."""
        query, retained = self._protected_query(
            account=account, operation_prompt="Remove Hussh One Source Library storage"
        )
        try:
            status = self._security.SecItemDelete(query)
            if status not in {self._SUCCESS, self._NOT_FOUND}:
                raise KeychainError(
                    f"Protected Keychain delete failed with OSStatus {status}."
                )
        finally:
            self._release_all(retained)
