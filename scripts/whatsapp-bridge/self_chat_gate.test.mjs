// SPDX-FileCopyrightText: 2026 Hushh Labs
// SPDX-License-Identifier: Apache-2.0
import test from 'node:test';
import assert from 'node:assert/strict';

import { isSelfChatJid, shouldRejectNonOwnerSelfChatEvent, shouldRejectFromMeEvent } from './self_chat_gate.js';

test('accepts an incoming self-chat addressed to the account phone JID', () => {
  assert.equal(isSelfChatJid({
    chatId: '15551234567@s.whatsapp.net',
    accountId: '15551234567:42@s.whatsapp.net',
  }), true);
});

test('accepts an incoming self-chat addressed to the account LID', () => {
  assert.equal(isSelfChatJid({
    chatId: '111600547700784@lid',
    accountId: '15551234567@s.whatsapp.net',
    accountLid: '111600547700784:9@lid',
  }), true);
});

test('never classifies strangers, groups, or allowlisted chats as self-chat', () => {
  const owner = { accountId: '15551234567@s.whatsapp.net', accountLid: '111600547700784@lid' };
  assert.equal(isSelfChatJid({ ...owner, chatId: '15557654321@s.whatsapp.net' }), false);
  assert.equal(isSelfChatJid({ ...owner, chatId: '120363040968035480@g.us', isGroup: true }), false);
  assert.equal(isSelfChatJid({ ...owner, chatId: '15551234567@s.whatsapp.net', isAllowlisted: true }), false);
});

test('self-chat rejection preserves capsule-specific entry points', () => {
  assert.equal(shouldRejectNonOwnerSelfChatEvent({
    mode: 'self-chat', isAllowlisted: false, isSelfChat: false, isCapsuleGroup: false,
  }), true);
  assert.equal(shouldRejectNonOwnerSelfChatEvent({
    mode: 'self-chat', isAllowlisted: false, isSelfChat: true, isCapsuleGroup: false,
  }), false);
  assert.equal(shouldRejectNonOwnerSelfChatEvent({
    mode: 'self-chat', isAllowlisted: false, isSelfChat: false, isCapsuleGroup: true,
  }), false);
});

test('fromMe in self-chat mode strictly rejects 1:1 DMs with other people and unallowlisted groups', () => {
  // 1:1 DM with another person: MUST BE REJECTED
  assert.equal(shouldRejectFromMeEvent({
    mode: 'self-chat', isGroup: false, isAllowlisted: false, isSelfChat: false,
  }), true);

  // Self-chat (Note to Self): MUST BE ACCEPTED
  assert.equal(shouldRejectFromMeEvent({
    mode: 'self-chat', isGroup: false, isAllowlisted: false, isSelfChat: true,
  }), false);

  // Allowlisted group: MUST BE ACCEPTED
  assert.equal(shouldRejectFromMeEvent({
    mode: 'self-chat', isGroup: true, isAllowlisted: true, isSelfChat: false,
  }), false);

  // Unallowlisted group: MUST BE REJECTED
  assert.equal(shouldRejectFromMeEvent({
    mode: 'self-chat', isGroup: true, isAllowlisted: false, isSelfChat: false,
  }), true);

  // Bot mode: ALL fromMe rejected (treated as echo-backs)
  assert.equal(shouldRejectFromMeEvent({
    mode: 'bot', isGroup: false, isAllowlisted: false, isSelfChat: false,
  }), true);
});
