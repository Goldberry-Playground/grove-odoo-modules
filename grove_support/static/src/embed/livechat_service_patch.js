/** @odoo-module **/

/**
 * One server conversation per temporary livechat thread (duplicate-chat fix).
 *
 * Odoo 19's LivechatService.persist() guards concurrent calls with
 * `_persistDeferred`, but clears that guard in `finally` as soon as the server
 * channel exists. The temporary thread is only swapped out later, and its
 * composer can keep focus after the swap. A second send from that composer
 * calls persist() on the still-transient thread with no guard, so
 * `get_session` runs again and the visitor gets a SECOND channel: two chat
 * windows, the bot asking for the email twice, and leads split across
 * conversations. Reproduced on prod 2026-10-29 (a second message ~5 s after
 * the first landed in a new channel) and locally against a cross-origin page.
 *
 * Fix: remember which saved thread each temporary thread became and hand that
 * same thread back on any later persist() of it. Keyed by the raw record, not
 * localId: every temporary livechat thread has id -1, so a localId key would
 * route a brand-new chat into the visitor's previous, ended one.
 */

import {LivechatService} from "@im_livechat/embed/common/livechat_service";
import {toRaw} from "@odoo/owl";
import {patch} from "@web/core/utils/patch";

const persistedByTemporaryThread = new WeakMap();

patch(LivechatService.prototype, {
    async persist(thread) {
        const key = thread?.isTransient ? toRaw(thread) : null;
        const known = key && persistedByTemporaryThread.get(key);
        if (known?.exists()) {
            return known;
        }
        const savedThread = await super.persist(...arguments);
        if (key && savedThread) {
            persistedByTemporaryThread.set(key, savedThread);
        }
        return savedThread;
    },
});
