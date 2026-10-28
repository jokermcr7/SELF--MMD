# Self Manager — Film Menu Edition

This build replaces the main self-manager menu with a 7-page navigation modeled on the supplied reference video. Pages include General, Auto Features, Security/Privacy, Modes/Appearance, Tools, Group/Message, and Games/Entertainment.

The menu has working page navigation (1–7, previous/next) and buttons are wired to existing project handlers or dedicated settings/actions. The self-login flow is intentionally unchanged.

Important Telegram limitations: a bot cannot receive arbitrary ordinary group text while Privacy Mode prevents it; arbitrary InlineKeyboard background colors are also controlled by Telegram. The panel trigger therefore cannot bypass Telegram platform restrictions.


## Update 2
- Fixed admin user search by phone number, including Persian/Arabic digits and common international phone formats.
- Search results now resolve to the existing full user-detail card and management buttons.
