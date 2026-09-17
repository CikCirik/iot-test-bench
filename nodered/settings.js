// Settings Node-RED minimal, cu login (adminAuth) generat automat din
// variabile de mediu la fiecare pornire - fara niciun pas manual de hash.
// Calea absoluta la bcryptjs: settings.js e incarcat de runtime-ul Node-RED
// dintr-un director diferit (/data, montat de noi), iar require() rezolva
// relativ la fisierul care il apeleaza - o cale relativa 'bcryptjs' NU s-ar
// gasi din /data; calea absoluta ocoleste complet problema.
const bcrypt = require('/usr/src/node-red/node_modules/bcryptjs');

const authUser = process.env.NODERED_AUTH_USER || 'admin';
const authPass = process.env.NODERED_AUTH_PASS || '';

module.exports = {
    flowFile: 'flows.json',
    uiPort: process.env.PORT || 1880,

    // Fara parola setata (NODERED_AUTH_PASS gol) = fara login, ca inainte.
    ...(authPass ? {
        adminAuth: {
            type: 'credentials',
            users: [{
                username: authUser,
                password: bcrypt.hashSync(authPass, 8),
                permissions: '*'
            }]
        }
    } : {}),

    logging: {
        console: {
            level: 'info',
            metrics: false,
            audit: false
        }
    },
    exportGlobalContextKeys: false,
    editorTheme: {
        projects: {
            enabled: false
        }
    }
};
