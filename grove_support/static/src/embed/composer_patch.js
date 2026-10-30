/** @odoo-module **/

/**
 * Empty the livechat text box after every send (stale-text fix).
 *
 * The textarea is bound with `t-model="props.composer.composerText"`. OWL only
 * writes that value to the DOM when it differs from the value it last
 * rendered. After the temporary->saved thread swap, typing in the livechat
 * composer no longer re-renders it, so the last rendered value stays "" and
 * `clear()` setting composerText back to "" is a no-op for the DOM: the sent
 * text stays in the box and the next message is typed onto the end of it
 * ("A" then "AB" on the wire). Seen on prod 2026-10-29; the model was cleared
 * and the posted body was right, only the textarea was stale.
 *
 * Fix: after clear(), write the cleared value into the textarea if the DOM
 * disagrees with the model. Only in the embed bundle, so backend Discuss is
 * untouched.
 */

import {Composer} from "@mail/core/common/composer";
import {patch} from "@web/core/utils/patch";

patch(Composer.prototype, {
    clear() {
        super.clear(...arguments);
        const textarea = this.ref?.el;
        const text = this.props.composer.composerText;
        if (textarea && textarea.value !== text) {
            textarea.value = text;
        }
    },
});
