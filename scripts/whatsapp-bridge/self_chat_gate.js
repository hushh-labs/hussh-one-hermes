// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
/**
 * Pure self-chat routing rules for the WhatsApp bridge.
 *
 * WhatsApp can represent the owner's account with either its phone JID or a
 * LID.  Incoming self-chat messages are not consistently marked `fromMe`, so
 * this classification must happen before the bridge's `fromMe` branches.
 */

function localJid(id) {
  return String(id || '')
    .replace(/:.*@/, '@')
    .replace(/@.*/, '')
    .trim();
}

export function isSelfChatJid({
  chatId,
  accountId,
  accountLid,
  isGroup = false,
  isAllowlisted = false,
}) {
  if (isGroup || isAllowlisted) return false;

  const chat = localJid(chatId);
  if (!chat) return false;

  const phone = localJid(accountId);
  const lid = localJid(accountLid);
  return Boolean((phone && chat === phone) || (lid && chat === lid));
}

export function shouldRejectNonOwnerSelfChatEvent({
  mode,
  isAllowlisted,
  isSelfChat,
  isCapsuleGroup,
}) {
  return mode === 'self-chat' && !isAllowlisted && !isSelfChat && !isCapsuleGroup;
}
