// Preloaded (node --require, through NODE_OPTIONS) into the patchright driver processes that
// ProfilePilot starts: see profilepilot/automation/driver.py. It applies the text patches of
// patchright_patches.json to patchright's driver bundle in memory while Node compiles it; nothing
// on disk changes. A patch whose text is not found exactly once stops the driver: a loud failure,
// never a silent one (driver.py checks the installed bundle before it starts a driver, too).
'use strict';

const Module = require('module');
const path = require('path');

const PATCHES = require(path.join(__dirname, 'patchright_patches.json'));

const compile = Module.prototype._compile;
Module.prototype._compile = function (content, filename) {
  const parts = filename.split(path.sep);
  if (parts.includes('patchright') && parts.includes('driver')) {
    for (const patch of PATCHES) {
      if (path.basename(filename) !== patch.file) continue;
      const at = content.indexOf(patch.find);
      if (at < 0 || content.indexOf(patch.find, at + 1) >= 0) {
        throw new Error('ProfilePilot: the patchright driver patch "' + patch.name + '" does not apply to ' + filename);
      }
      content = content.slice(0, at) + patch.replace + content.slice(at + patch.find.length);
    }
  }
  return compile.call(this, content, filename);
};
