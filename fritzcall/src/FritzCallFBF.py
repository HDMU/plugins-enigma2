# -*- coding: utf-8 -*-
'''
Created on 30.09.2012
$Author: michael $
$Revision: 776 $
$Date: 2013-05-11 13:15:24 +0200 (Sat, 11 May 2013) $
$Id: FritzCallFBF.py 776 2013-05-11 11:15:24Z michael $
'''

# pylint: disable=W1401,E0611,F0401

from . import _, __, debug #@UnresolvedImport
from plugin import config, fritzbox, stripCbCPrefix, resolveNumberWithAvon, FBF_IN_CALLS, FBF_OUT_CALLS, FBF_MISSED_CALLS
from Tools import Notifications
from Screens.MessageBox import MessageBox
from twisted.web.client import getPage #@UnresolvedImport
from nrzuname import html2unicode

from urllib import urlencode 
import re, time, hashlib

FBF_boxInfo = 0
FBF_upTime = 1
FBF_ipAddress = 2
FBF_wlanState = 3
FBF_dslState = 4
FBF_tamActive = 5
FBF_dectActive = 6
FBF_faxActive = 7
FBF_rufumlActive = 8

def resolveNumber(number, default=None, phonebook=None):
	if number.isdigit():
		if config.plugins.FritzCall.internal.value and len(number) > 3 and number[0] == "0":
			number = number[1:]
		# strip CbC prefix
		number = stripCbCPrefix(number, config.plugins.FritzCall.country.value)
		if config.plugins.FritzCall.prefix.value and number and number[0] != '0':		# should only happen for outgoing
			number = config.plugins.FritzCall.prefix.value + number
		name = None
		if phonebook:
			name = phonebook.search(number)
		if name:
			#===========================================================
			# found = re.match('(.*?)\n.*', name)
			# if found:
			#	name = found.group(1)
			#===========================================================
			end = name.find('\n')
			if end != -1:
				name = name[:end]
			number = name
		elif default:
			number = default
		else:
			name = resolveNumberWithAvon(number, config.plugins.FritzCall.country.value)
			if name:
				number = number + ' ' + name
	elif number == "":
		number = _("UNKNOWN")
	# if len(number) > 20: number = number[:20]
	return number

def cleanNumber(number):
	number = number.replace('(','').replace(')','').replace(' ','').replace('-','')
	if number[0] == '+':
		number = '00' + number[1:]
	if number.startswith(config.plugins.FritzCall.country.value):
		number = '0' + number[len(config.plugins.FritzCall.country.value):]
	return number
		
class FritzCallFBF:
	def __init__(self):
		debug("[FritzCallFBF] __init__")
		self._callScreen = None
		self._md5LoginTimestamp = None
		self._md5Sid = '0000000000000000'
		self._callTimestamp = 0
		self._callList = []
		self._callType = config.plugins.FritzCall.fbfCalls.value
		self.info = None # (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive)
		self.getInfo(None)
		self.blacklist = ([], [])
		self.readBlacklist()
		self.phonebook = None
		self._phoneBookID = 0
		self.phonebooksFBF = []

	def _notify(self, text):
		debug("[FritzCallFBF] notify: " + text)
		self._md5LoginTimestamp = None
		if self._callScreen:
			debug("[FritzCallFBF] notify: try to close callScreen")
			self._callScreen.close()
			self._callScreen = None
		Notifications.AddNotification(MessageBox, text, type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
			
	def _login(self, callback=None):
		debug("[FritzCallFBF] _login")
		if self._callScreen:
			self._callScreen.updateStatus(_("login"))
		if self._md5LoginTimestamp and ((time.time() - self._md5LoginTimestamp) < float(9.5*60)) and self._md5Sid != '0000000000000000': # new login after 9.5 minutes inactivity 
			debug("[FritzCallFBF] _login: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
			self._md5LoginTimestamp = time.time()
			callback(None)
		else:
			debug("[FritzCallFBF] _login: not logged in or outdated login")
			# http://fritz.box/cgi-bin/webcm?getpage=../html/login_sid.xml
			parms = urlencode({'getpage':'../html/login_sid.xml'})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF] _login: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addCallback(lambda x: self._md5Login(callback,x)).addErrback(lambda x:self._oldLogin(callback,x))

	def _oldLogin(self, callback, error): 
		debug("[FritzCallFBF] _oldLogin: " + repr(error))
		self._md5LoginTimestamp = None
		if config.plugins.FritzCall.password.value != "":
			parms = "login:command/password=%s" % (config.plugins.FritzCall.password.value)
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF] _oldLogin: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addCallback(self._gotPageLogin).addCallback(callback).addErrback(self._errorLogin)
		elif callback:
			debug("[FritzCallFBF] _oldLogin: no password, calling " + repr(callback))
			callback(None)

	def _md5Login(self, callback, sidXml):
		def buildResponse(challenge, text):
			debug("[FritzCallFBF] _md5Login7buildResponse: challenge: " + challenge + ' text: ' + __(text))
			text = (challenge + '-' + text).decode('utf-8','ignore').encode('utf-16-le')
			for i in range(len(text)):
				if ord(text[i]) > 255:
					text[i] = '.'
			md5 = hashlib.md5()
			md5.update(text)
			debug("[FritzCallFBF] md5Login/buildResponse: " + md5.hexdigest())
			return challenge + '-' + md5.hexdigest()

		debug("[FritzCallFBF] _md5Login")
		found = re.match('.*<SID>([^<]*)</SID>', sidXml, re.S)
		if found:
			self._md5Sid = found.group(1)
			debug("[FritzCallFBF] _md5Login: SID "+ self._md5Sid)
		else:
			debug("[FritzCallFBF] _md5Login: no sid! That must be an old firmware.")
			self._oldLogin(callback, 'No error')
			return

		debug("[FritzCallFBF] _md5Login: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
		self._md5LoginTimestamp = time.time()
		if sidXml.find('<iswriteaccess>0</iswriteaccess>') != -1:
			debug("[FritzCallFBF] _md5Login: logging in")
			found = re.match('.*<Challenge>([^<]*)</Challenge>', sidXml, re.S)
			if found:
				challenge = found.group(1)
				debug("[FritzCallFBF] _md5Login: challenge " + challenge)
			else:
				challenge = None
				debug("[FritzCallFBF] _md5Login: login necessary and no challenge! That is terribly wrong.")
			parms = urlencode({
							'getpage':'../html/de/menus/menu2.html', # 'var:pagename':'home', 'var:menu':'home', 
							'login:command/response': buildResponse(challenge, config.plugins.FritzCall.password.value),
							})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF] _md5Login: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addCallback(self._gotPageLogin).addCallback(callback).addErrback(self._errorLogin)
		elif callback: # we assume value 1 here, no login necessary
			debug("[FritzCallFBF] _md5Login: no login necessary")
			callback(None)

	def _gotPageLogin(self, html):
		if self._callScreen:
			self._callScreen.updateStatus(_("login verification"))
		debug("[FritzCallFBF] _gotPageLogin: verify login")
		start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
		if start != -1:
			start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
			text = _("FRITZ!Box - Error logging in\n\n") + html[start : html.find('</p>', start)]
			self._notify(text)
		else:
			if self._callScreen:
				self._callScreen.updateStatus(_("login ok"))

		found = re.match('.*<input type="hidden" name="sid" value="([^\"]*)"', html, re.S)
		if found:
			self._md5Sid = found.group(1)
			debug("[FritzCallFBF] _gotPageLogin: found sid: " + self._md5Sid)

	def _errorLogin(self, error):
		global fritzbox
		debug("[FritzCallFBF] _errorLogin: %s" % (error))
		text = _("FRITZ!Box - Error logging in: %s\nDisabling plugin.") % error.getErrorMessage()
		# config.plugins.FritzCall.enable.value = False
		fritzbox = None
		self._notify(text)

	def _logout(self):
		if self._md5LoginTimestamp:
			self._md5LoginTimestamp = None
			parms = urlencode({
							'getpage':'../html/de/menus/menu2.html', # 'var:pagename':'home', 'var:menu':'home', 
							'login:command/logout':'bye bye Fritz'
							})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF] logout: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addErrback(self._errorLogout)

	def _errorLogout(self, error):
		debug("[FritzCallFBF] _errorLogout: %s" % (error))
		text = _("FRITZ!Box - Error logging out: %s") % error.getErrorMessage()
		self._notify(text)

	def loadFritzBoxPhonebook(self, phonebook):
		debug("[FritzCallFBF] loadFritzBoxPhonebook")
		if config.plugins.FritzCall.fritzphonebook.value:
			self.phonebook = phonebook
			self._phoneBookID = '0'
			debug("[FritzCallFBF] loadFritzBoxPhonebook: logging in")
			self._login(self._loadFritzBoxPhonebook)

	def _loadFritzBoxPhonebook(self, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorLoad('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._notify('Login: ' + html[start, html.find('</p>', start)])
				return
		parms = urlencode({
						'getpage':'../html/de/menus/menu2.html',
						'var:lang':'de',
						'var:pagename':'fonbuch',
						'var:menu':'fon',
						'sid':self._md5Sid,
						'telcfg:settings/Phonebook/Books/Select':self._phoneBookID, # this selects always the first phonbook first
						})
		url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF] _loadFritzBoxPhonebook: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._parseFritzBoxPhonebook).addErrback(self._errorLoad)

	def _parseFritzBoxPhonebook(self, html):

		# debug("[FritzCallFBF] _parseFritzBoxPhonebook")

		# first, let us get the charset
		found = re.match('.*<meta http-equiv=content-type content="text/html; charset=([^"]*)">', html, re.S)
		if found:
			charset = found.group(1)
			debug("[FritzCallFBF] _parseFritzBoxPhonebook: found charset: " + charset)
			html = html2unicode(html.replace(chr(0xf6),'').decode(charset)).encode('utf-8')
		else: # this is kind of emergency conversion...
			try:
				debug("[FritzCallFBF] _parseFritzBoxPhonebook: try charset utf-8")
				charset = 'utf-8'
				html = html2unicode(html.decode('utf-8')).encode('utf-8') # this looks silly, but has to be
			except UnicodeDecodeError:
				debug("[FritzCallFBF] _parseFritzBoxPhonebook: try charset iso-8859-1")
				charset = 'iso-8859-1'
				html = html2unicode(html.decode('iso-8859-1')).encode('utf-8') # this looks silly, but has to be

		# if re.search('document.write\(TrFon1\(\)', html):
		if html.find('document.write(TrFon1()') != -1:
			#===============================================================================
			#				 New Style: 7270 (FW 54.04.58, 54.04.63-11941, 54.04.70, 54.04.74-14371, 54.04.76, PHONE Labor 54.04.80-16624)
			#							7170 (FW 29.04.70) 22.03.2009
			#							7141 (FW 40.04.68) 22.03.2009
			#  We expect one line with
			#   TrFonName(Entry umber, Name, ???, Path to picture)
			#  followed by several lines with
			#	TrFonNr(Type,Number,Shortcut,Vanity), which all belong to the name in TrFonName.
			# 
			#  Photo could be fetched with http://192.168.0.1/lua/photo.lua?photo=<Path to picture[7:]&sid=????
			#===============================================================================
			debug("[FritzCallFBF] _parseFritzBoxPhonebook: discovered newer firmware")
			found = re.match('.*<input type="hidden" name="telcfg:settings/Phonebook/Books/Name\d+" value="(?:' + config.plugins.FritzCall.fritzphonebookName.value +')" id="uiPostPhonebookName\d+" disabled>\s*<input type="hidden" name="telcfg:settings/Phonebook/Books/Id\d+" value="(\d+)" id="uiPostPhonebookId\d+" disabled>', html, re.S)
			if found:
				phoneBookID = found.group(1)
				debug("[FritzCallFBF] _parseFritzBoxPhonebook: found dreambox phonebook with id: " + phoneBookID)
				if self._phoneBookID != phoneBookID:
					self._phoneBookID = phoneBookID
					debug("[FritzCallFBF] _parseFritzBoxPhonebook: reload phonebook")
					self._loadFritzBoxPhonebook(None) # reload with dreambox phonebook
					return

			entrymask = re.compile('(TrFonName\("[^"]+", "[^"]+", "[^"]*"(?:, "[^"]*")?\);.*?)document.write\(TrFon1\(\)', re.S)
			entries = entrymask.finditer(html)
			for entry in entries:
				# TrFonName (id, name, category)
				# TODO: replace re.match?
				found = re.match('TrFonName\("[^"]*", "([^"]+)", "[^"]*"(?:, "[^"]*")?\);', entry.group(1))
				if found:
					# debug("[FritzCallFBF] _parseFritzBoxPhonebook: name: %s" %found.group(1))
					name = found.group(1).replace(',','').strip()
				else:
					debug("[FritzCallFBF] _parseFritzBoxPhonebook: could not find name")
					continue
				# TrFonNr (type, rufnr, code, vanity)
				detailmask = re.compile('TrFonNr\("([^"]*)", "([^"]*)", "([^"]*)", "([^"]*)"\);', re.S)
				details = detailmask.finditer(entry.group(1))
				for found in details:
					thisnumber = found.group(2).strip()
					if not thisnumber:
						debug("[FritzCallFBF] Ignoring entry with empty number for '''%s'''" % (__(name)))
						continue
					else:
						thisname = name
						callType = found.group(1)
						if config.plugins.FritzCall.showType.value:
							if callType == "mobile":
								thisname = thisname + " (" + _("mobile") + ")"
							elif callType == "home":
								thisname = thisname + " (" + _("home") + ")"
							elif callType == "work":
								thisname = thisname + " (" + _("work") + ")"

						if config.plugins.FritzCall.showShortcut.value and found.group(3):
							thisname = thisname + ", " + _("Shortcut") + ": " + found.group(3)
						if config.plugins.FritzCall.showVanity.value and found.group(4):
							thisname = thisname + ", " + _("Vanity") + ": " + found.group(4)

						thisnumber = cleanNumber(thisnumber)
						# Beware: strings in phonebook.phonebook have to be in utf-8!
						if not self.phonebook.phonebook.has_key(thisnumber):
							debug("[FritzCallFBF] Adding '''%s''' with '''%s'''" % (__(thisname.strip()), __(thisnumber, False)))
							self.phonebook.phonebook[thisnumber] = thisname
						else:
							pass
							# debug("[FritzCallFBF] Ignoring '''%s''' with '''%s'''" % (thisname.strip(), thisnumber))

		# elif re.search('document.write\(TrFon\(', html):
		elif html.find('document.write(TrFon(') != -1:
			#===============================================================================
			#				Old Style: 7050 (FW 14.04.33)
			#	We expect one line with TrFon(No,Name,Number,Shortcut,Vanity)
			#   Encoding should be plain Ascii...
			#===============================================================================				
			entrymask = re.compile('TrFon\("[^"]*", "([^"]*)", "([^"]*)", "([^"]*)", "([^"]*)"\)', re.S)
			entries = entrymask.finditer(html)
			for found in entries:
				name = found.group(1).strip().replace(',','')
				# debug("[FritzCallFBF] pos: %s name: %s" %(found.group(0),name))
				thisnumber = found.group(2).strip()
				if config.plugins.FritzCall.showShortcut.value and found.group(3):
					name = name + ", " + _("Shortcut") + ": " + found.group(3)
				if config.plugins.FritzCall.showVanity.value and found.group(4):
					name = name + ", " + _("Vanity") + ": " + found.group(4)
				if thisnumber:
					# name = name.encode('utf-8')
					# Beware: strings in phonebook.phonebook have to be in utf-8!
					if not self.phonebook.phonebook.has_key(thisnumber):
						debug("[FritzCallFBF] Adding '''%s''' with '''%s'''" % (name, __(thisnumber)))
						self.phonebook.phonebook[thisnumber] = name
					else:
						debug("[FritzCallFBF] Ignoring '''%s''' with '''%s'''" % (name, __(thisnumber)))
				else:
					debug("[FritzCallFBF] ignoring empty number for %s" % name)
				continue
		elif self._md5Sid == '0000000000000000': # retry, it could be a race condition
			debug("[FritzCallFBF] _parseFritzBoxPhonebook: retry loading phonebook")
			self.loadFritzBoxPhonebook(self.phonebook)
		else:
			debug("[FritzCallFBF] _parseFritzBoxPhonebook: could not read FBF phonebook; wrong version?")
			self._notify(_("Could not read FRITZ!Box phonebook; wrong version?"))

	def _errorLoad(self, error):
		debug("[FritzCallFBF] _errorLoad: %s" % (error))
		text = _("FRITZ!Box - Could not load phonebook: %s") % error.getErrorMessage()
		self._notify(text)

	def getCalls(self, callScreen, callback, callType):
		#
		# call sequence must be:
		# - login
		# - getPage -> _gotPageLogin
		# - loginCallback (_getCalls)
		# - getPage -> _getCalls1
		debug("[FritzCallFBF] getCalls")
		self._callScreen = callScreen
		self._callType = callType
		if (time.time() - self._callTimestamp) > 180: 
			debug("[FritzCallFBF] getCalls: outdated data, login and get new ones: " + time.ctime(self._callTimestamp) + " time: " + time.ctime())
			self._callTimestamp = time.time()
			self._login(lambda x:self._getCalls(callback, x))
		elif not self._callList:
			debug("[FritzCallFBF] getCalls: time is ok, but no callList")
			self._getCalls1(callback)
		else:
			debug("[FritzCallFBF] getCalls: time is ok, callList is ok")
			self._gotPageCalls(callback)

	def _getCalls(self, callback, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorCalls('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._notify('Login: ' + html[start, html.find('</p>', start)])
				return
		#
		# we need this to fill Anrufliste.csv
		# http://repeater1/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:lang=de&var:menu=fon&var:pagename=foncalls
		#
		debug("[FritzCallFBF] _getCalls")
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	text = _("FRITZ!Box - Error logging in: %s") + found.group(1)
			#	self._notify(text)
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._notify(_("FRITZ!Box - Error logging in: %s") + html[start, html.find('</p>', start)])
				return

		if self._callScreen:
			self._callScreen.updateStatus(_("preparing"))
		parms = urlencode({'getpage':'../html/de/menus/menu2.html', 'var:lang':'de', 'var:pagename':'foncalls', 'var:menu':'fon', 'sid':self._md5Sid})
		url = "http://%s/cgi-bin/webcm?%s" % (config.plugins.FritzCall.hostname.value, parms)
		getPage(url).addCallback(lambda x:self._getCalls1(callback)).addErrback(self._errorCalls) #@UnusedVariable # pylint: disable=W0613

	def _getCalls1(self, callback):
		#
		# finally we should have successfully lgged in and filled the csv
		#
		debug("[FritzCallFBF] _getCalls1")
		if self._callScreen:
			self._callScreen.updateStatus(_("finishing"))
		parms = urlencode({'getpage':'../html/de/FRITZ!Box_Anrufliste.csv', 'sid':self._md5Sid})
		url = "http://%s/cgi-bin/webcm?%s" % (config.plugins.FritzCall.hostname.value, parms)
		getPage(url).addCallback(lambda x:self._gotPageCalls(callback, x)).addErrback(self._errorCalls)

	def _gotPageCalls(self, callback, csv=""):

		if csv:
			debug("[FritzCallFBF] _gotPageCalls: got csv, setting callList")
			if self._callScreen:
				self._callScreen.updateStatus(_("done"))
			if csv.find('Melden Sie sich mit dem Kennwort der FRITZ!Box an') != -1:
				text = _("You need to set the password of the FRITZ!Box\nin the configuration dialog to display calls\n\nIt could be a communication issue, just try again.")
				# self.session.open(MessageBox, text, MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
				self._notify(text)
				return

			csv = csv.decode('iso-8859-1', 'replace').encode('utf-8', 'replace')
			lines = csv.splitlines()
			self._callList = lines
		elif self._callList:
			debug("[FritzCallFBF] _gotPageCalls: got no csv, but have callList")
			if self._callScreen:
				self._callScreen.updateStatus(_("done, using last list"))
			lines = self._callList
		else:
			debug("[FritzCallFBF] _gotPageCalls: Could not get call list; wrong version?")
			self._notify(_("Could not get call list; wrong version?"))
			return
			
		callListL = []
		if config.plugins.FritzCall.filter.value and config.plugins.FritzCall.filterCallList.value:
			filtermsns = map(lambda x: x.strip(), config.plugins.FritzCall.filtermsn.value.split(","))
			# TODO: scramble filtermsns
			debug("[FritzCallFBF] _gotPageCalls: filtermsns %s" % (repr(filtermsns)))

		# Typ;Datum;Name;Rufnummer;Nebenstelle;Eigene Rufnummer;Dauer
		# 0  ;1	   ;2   ;3		  ;4		  ;5			   ;6
		lines = map(lambda line: line.split(';'), lines)
		lines = filter(lambda line: (len(line)==7 and (line[0]=="Typ" or self._callType == '.' or line[0] == self._callType)), lines)

		for line in lines:
			# debug("[FritzCallFBF] _gotPageCalls: line %s" % (line))
			direct = line[0]
			date = line[1]
			length = line[6]
			if config.plugins.FritzCall.phonebook.value and line[2]:
				remote = resolveNumber(line[3], line[2] + " (FBF)", self.phonebook)
			else:
				remote = resolveNumber(line[3], line[2], self.phonebook)
			here = line[5]
			start = here.find('Internet: ')
			if start != -1:
				start += len('Internet: ')
				here = here[start:]
			else:
				here = line[5]
			if direct != "Typ" and config.plugins.FritzCall.filter.value and config.plugins.FritzCall.filterCallList.value:
				# debug("[FritzCallFBF] _gotPageCalls: check %s" % (here))
				if here not in filtermsns:
					# debug("[FritzCallFBF] _gotPageCalls: skip %s" % (here))
					continue
			here = resolveNumber(here, line[4], self.phonebook)

			number = stripCbCPrefix(line[3], config.plugins.FritzCall.country.value)
			if config.plugins.FritzCall.prefix.value and number and number[0] != '0':		# should only happen for outgoing
				number = config.plugins.FritzCall.prefix.value + number
			callListL.append((number, date, direct, remote, length, here))

		if callback:
			# debug("[FritzCallFBF] _gotPageCalls call callback with\n" + repr(callListL))
			callback(callListL)
		self._callScreen = None

	def _errorCalls(self, error):
		debug("[FritzCallFBF] _errorCalls: %s" % (error))
		text = _("FRITZ!Box - Could not load calls: %s") % error.getErrorMessage()
		self._notify(text)

	def dial(self, number):
		''' initiate a call to number '''
		self._login(lambda x: self._dial(number, x))
		
	def _dial(self, number, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorDial('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorDial('Login: ' + html[start, html.find('</p>', start)])
				return
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:pagename':'fonbuch',
			'var:menu':'home',
			'telcfg:settings/UseClickToDial':'1',
			'telcfg:settings/DialPort':config.plugins.FritzCall.extension.value,
			'telcfg:command/Dial':number,
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] dial url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okDial).addErrback(self._errorDial)

	def _okDial(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF] okDial")

	def _errorDial(self, error):
		debug("[FritzCallFBF] errorDial: $s" % error)
		text = _("FRITZ!Box - Dialling failed: %s") % error.getErrorMessage()
		self._notify(text)

	def changeWLAN(self, statusWLAN):
		''' get status info from FBF '''
		debug("[FritzCallFBF] changeWLAN start")
		if not statusWLAN or (statusWLAN != '1' and statusWLAN != '0'):
			return
		self._login(lambda x: self._changeWLAN(statusWLAN, x))
		
	def _changeWLAN(self, statusWLAN, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorChangeWLAN('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorChangeWLAN('Login: ' + html[start, html.find('</p>', start)])
				return
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:lang':'de',
			'var:pagename':'wlan',
			'var:menu':'wlan',
			'wlan:settings/ap_enabled':str(statusWLAN),
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] changeWLAN url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okChangeWLAN).addErrback(self._errorChangeWLAN)

	def _okChangeWLAN(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF] _okChangeWLAN")

	def _errorChangeWLAN(self, error):
		debug("[FritzCallFBF] _errorChangeWLAN: $s" % error)
		text = _("FRITZ!Box - Failed changing WLAN: %s") % error.getErrorMessage()
		self._notify(text)

	def changeMailbox(self, whichMailbox):
		''' switch mailbox on/off '''
		debug("[FritzCallFBF] changeMailbox start: " + str(whichMailbox))
		self._login(lambda x: self._changeMailbox(whichMailbox, x))

	def _changeMailbox(self, whichMailbox, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorChangeMailbox('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorChangeMailbox('Login: ' + html[start, html.find('</p>', start)])
				return
		debug("[FritzCallFBF] _changeMailbox")
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		if whichMailbox == -1:
			for i in range(5):
				if self.info[FBF_tamActive][i+1]:
					state = '0'
				else:
					state = '1'
				parms = urlencode({
					'tam:settings/TAM'+str(i)+'/Active':state,
					'sid':self._md5Sid
					})
				debug("[FritzCallFBF] changeMailbox url: '" + url + "' parms: '" + parms + "'")
				getPage(url,
					method="POST",
					agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
					headers={
							'Content-Type': "application/x-www-form-urlencoded",
							'Content-Length': str(len(parms))},
					postdata=parms).addCallback(self._okChangeMailbox).addErrback(self._errorChangeMailbox)
		elif whichMailbox > 4:
			debug("[FritzCallFBF] changeMailbox invalid mailbox number")
		else:
			if self.info[FBF_tamActive][whichMailbox+1]:
				state = '0'
			else:
				state = '1'
			parms = urlencode({
				'tam:settings/TAM'+str(whichMailbox)+'/Active':state,
				'sid':self._md5Sid
				})
			debug("[FritzCallFBF] changeMailbox url: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={
						'Content-Type': "application/x-www-form-urlencoded",
						'Content-Length': str(len(parms))},
				postdata=parms).addCallback(self._okChangeMailbox).addErrback(self._errorChangeMailbox)

	def _okChangeMailbox(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF] _okChangeMailbox")

	def _errorChangeMailbox(self, error):
		debug("[FritzCallFBF] _errorChangeMailbox: $s" % error)
		text = _("FRITZ!Box - Failed changing Mailbox: %s") % error.getErrorMessage()
		self._notify(text)

	def getInfo(self, callback):
		''' get status info from FBF '''
		debug("[FritzCallFBF] getInfo")
		self._login(lambda x:self._getInfo(callback, x))
		
	def _getInfo(self, callback, html):
		# http://192.168.178.1/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:lang=de&var:pagename=home&var:menu=home
		debug("[FritzCallFBF] _getInfo: verify login")
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorGetInfo('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorGetInfo('Login: ' + html[start, html.find('</p>', start)])
				return

		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:lang':'de',
			'var:pagename':'home',
			'var:menu':'home',
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] _getInfo url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(lambda x:self._okGetInfo(callback,x)).addErrback(self._errorGetInfo)

	def _okGetInfo(self, callback, html):
		def readInfo(html):
			if self.info:
				(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = self.info
			else:
				(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = (None, None, None, None, None, None, None, None, None)

			debug("[FritzCallFBF] _okGetInfo/readinfo")
			found = re.match('.*<table class="tborder" id="tProdukt">\s*<tr>\s*<td style="padding-top:2px;">([^<]*)</td>\s*<td style="padding-top:2px;text-align:right;">\s*([^\s]*)\s*</td>', html, re.S)
			if found:
				boxInfo = found.group(1)+ ', ' + found.group(2)
				boxInfo = boxInfo.replace('&nbsp;',' ')
				# debug("[FritzCallFBF] _okGetInfo Boxinfo: " + boxInfo)
			else:
				found = re.match('.*<p class="ac">([^<]*)</p>', html, re.S)
				if found:
					# debug("[FritzCallFBF] _okGetInfo Boxinfo: " + found.group(1))
					boxInfo = found.group(1)

			if html.find('home_coninf.txt') != -1:
				url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
				parms = urlencode({
					'getpage':'../html/de/home/home_coninf.txt',
					'sid':self._md5Sid
					})
				# debug("[FritzCallFBF] get coninfo: url: '" + url + "' parms: '" + parms + "'")
				getPage(url,
					method="POST",
					agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
					headers={
							'Content-Type': "application/x-www-form-urlencoded",
							'Content-Length': str(len(parms))},
					postdata=parms).addCallback(lambda x:self._okSetConInfo(callback,x)).addErrback(self._errorGetInfo)
			else:
				found = re.match('.*if \(isNaN\(jetzt\)\)\s*return "";\s*var str = "([^"]*)";', html, re.S)
				if found:
					# debug("[FritzCallFBF] _okGetInfo Uptime: " + found.group(1))
					upTime = found.group(1)
				else:
					found = re.match('.*str = g_pppSeit \+"([^<]*)<br>"\+mldIpAdr;', html, re.S)
					if found:
						# debug("[FritzCallFBF] _okGetInfo Uptime: " + found.group(1))
						upTime = found.group(1)
	
				found = re.match(".*IpAdrDisplay\('([.\d]+)'\)", html, re.S)
				if found:
					# debug("[FritzCallFBF] _okGetInfo IpAdrDisplay: " + found.group(1))
					ipAddress = found.group(1)

			if html.find('g_tamActive') != -1:
				entries = re.compile('if \("(\d)" == "1"\) {\s*g_tamActive \+= 1;\s*}', re.S).finditer(html)
				tamActive = [0, False, False, False, False, False]
				i = 1
				for entry in entries:
					state = entry.group(1)
					if state == '1':
						tamActive[0] += 1
						tamActive[i] = True
					i += 1
				# debug("[FritzCallFBF] _okGetInfo tamActive: " + str(tamActive))
		
			if html.find('home_dect.txt') != -1:
				url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
				parms = urlencode({
					'getpage':'../html/de/home/home_dect.txt',
					'sid':self._md5Sid
					})
				# debug("[FritzCallFBF] get coninfo: url: '" + url + "' parms: '" + parms + "'")
				getPage(url,
					method="POST",
					agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
					headers={
							'Content-Type': "application/x-www-form-urlencoded",
							'Content-Length': str(len(parms))},
					postdata=parms).addCallback(lambda x:self._okSetDect(callback,x)).addErrback(self._errorGetInfo)
			else:
				if html.find('countDect2') != -1:
					entries = re.compile('if \("1" == "1"\) countDect2\+\+;', re.S).findall(html)
					dectActive = len(entries)
					# debug("[FritzCallFBF] _okGetInfo dectActive: " + str(dectActive))

			found = re.match('.*var g_intFaxActive = "0";\s*if \("1" != ""\) {\s*g_intFaxActive = "1";\s*}\s*', html, re.S)
			if found:
				faxActive = True
				# debug("[FritzCallFBF] _okGetInfo faxActive")

			if html.find('cntRufumleitung') != -1:
				entries = re.compile('mode = "1";\s*ziel = "[^"]+";\s*if \(mode == "1" \|\| ziel != ""\)\s*{\s*g_RufumleitungAktiv = true;', re.S).findall(html)
				rufumlActive = len(entries)
				entries = re.compile('if \("([^"]*)"=="([^"]*)"\) isAllIncoming\+\+;', re.S).finditer(html)
				isAllIncoming = 0
				for entry in entries:
					# debug("[FritzCallFBF] _okGetInfo rufumlActive add isAllIncoming")
					if entry.group(1) == entry.group(2):
						isAllIncoming += 1
				if isAllIncoming == 2 and rufumlActive > 0:
					rufumlActive -= 1
				# debug("[FritzCallFBF] _okGetInfo rufumlActive: " + str(rufumlActive))

			# /cgi-bin/webcm?getpage=../html/de/home/home_dsl.txt
			# alternative through: fritz.box/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:menu=internet&var:pagename=overview
			# { "dsl_carrier_state": "5", "umts_enabled": "0", "ata_mode": "0", "isusbgsm": "", "dsl_ds_nrate": "3130", "dsl_us_nrate": "448", "hint_dsl_no_cable": "0", "wds_enabled": "0", "wds_hop": "0", "isata": "" } 
			if html.find('home_dsl.txt') != -1:
				url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
				parms = urlencode({
					'getpage':'../html/de/home/home_dsl.txt',
					'sid':self._md5Sid
					})
				# debug("[FritzCallFBF] get dsl state: url: '" + url + "' parms: '" + parms + "'")
				getPage(url,
					method="POST",
					agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
					headers={
							'Content-Type': "application/x-www-form-urlencoded",
							'Content-Length': str(len(parms))},
					postdata=parms).addCallback(lambda x:self._okSetDslState(callback,x)).addErrback(self._errorGetInfo)
			else:
				found = re.match('.*function DslStateDisplay \(state\){\s*var state = "(\d+)";', html, re.S)
				if found:
					# debug("[FritzCallFBF] _okGetInfo DslState: " + found.group(1))
					dslState = [ found.group(1), None ] # state, speed
					found = re.match('.*function DslStateDisplay \(state\){\s*var state = "\d+";.*?if \("3130" != "0"\) str = "([^"]*)";', html, re.S)
					if found:
						# debug("[FritzCallFBF] _okGetInfo DslSpeed: " + found.group(1).strip())
						dslState[1] = found.group(1).strip()
		
			# /cgi-bin/webcm?getpage=../html/de/home/home_wlan.txt
			# { "ap_enabled": "1", "active_stations": "0", "encryption": "4", "wireless_stickandsurf_enabled": "0", "is_macfilter_active": "0", "wmm_enabled": "1", "wlan_state": [ "end" ] }
			if html.find('home_wlan.txt') != -1:
				url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
				parms = urlencode({
					'getpage':'../html/de/home/home_wlan.txt',
					'sid':self._md5Sid
					})
				# debug("[FritzCallFBF] get wlan state: url: '" + url + "' parms: '" + parms + "'")
				getPage(url,
					method="POST",
					agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
					headers={
							'Content-Type': "application/x-www-form-urlencoded",
							'Content-Length': str(len(parms))},
					postdata=parms).addCallback(lambda x:self._okSetWlanState(callback,x)).addErrback(self._errorGetInfo)
			else:
				found = re.match('.*function WlanStateLed \(state\){.*?return StateLed\("(\d+)"\);\s*}', html, re.S)
				if found:
					# debug("[FritzCallFBF] _okGetInfo WlanState: " + found.group(1))
					wlanState = [ found.group(1), 0, 0 ] # state, encryption, number of devices
					found = re.match('.*var (?:g_)?encryption = "(\d+)";', html, re.S)
					if found:
						# debug("[FritzCallFBF] _okGetInfo WlanEncrypt: " + found.group(1))
						wlanState[1] = found.group(1)

			return (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)

		debug("[FritzCallFBF] _okGetInfo")
		info = readInfo(html)
		debug("[FritzCallFBF] _okGetInfo info: " + str(info))
		self.info = info
		if callback:
			callback(info)

	def _okSetDect(self, callback, html):
		# debug("[FritzCallFBF] _okSetDect: " + html)
		# found = re.match('.*"connection_status":"(\d+)".*"connection_ip":"([.\d]+)".*"connection_detail":"([^"]+)".*"connection_uptime":"([^"]+)"', html, re.S)
		if html.find('"dect_enabled": "1"') != -1:
			# debug("[FritzCallFBF] _okSetDect: dect_enabled")
			found = re.match('.*"dect_device_list":.*\[([^\]]*)\]', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetDect: dect_device_list: %s" %(found.group(1)))
				entries = re.compile('"1"', re.S).findall(found.group(1))
				dectActive = len(entries)
				(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dummy, faxActive, rufumlActive) = self.info
				self.info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
				debug("[FritzCallFBF] _okSetDect info: " + str(self.info))
		if callback:
			callback(self.info)

	def _okSetConInfo(self, callback, html):
		# debug("[FritzCallFBF] _okSetConInfo: " + html)
		# found = re.match('.*"connection_status":"(\d+)".*"connection_ip":"([.\d]+)".*"connection_detail":"([^"]+)".*"connection_uptime":"([^"]+)"', html, re.S)
		found = re.match('.*"connection_ip": "([.\d]+)".*"connection_uptime": "([^"]+)"', html, re.S)
		if found:
			# debug("[FritzCallFBF] _okSetConInfo: connection_ip: %s upTime: %s" %( found.group(1), found.group(2)))
			ipAddress = found.group(1)
			upTime = found.group(2)
			(boxInfo, dummy, dummy, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = self.info
			self.info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
			debug("[FritzCallFBF] _okSetWlanState info: " + str(self.info))
		else:
			found = re.match('.*_ip": "([.\d]+)".*"connection_uptime": "([^"]+)"', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetConInfo: _ip: %s upTime: %s" %( found.group(1), found.group(2)))
				ipAddress = found.group(1)
				upTime = found.group(2)
				(boxInfo, dummy, dummy, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = self.info
				self.info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
				debug("[FritzCallFBF] _okSetWlanState info: " + str(self.info))
		if callback:
			callback(self.info)

	def _okSetWlanState(self, callback, html):
		# debug("[FritzCallFBF] _okSetWlanState: " + html)
		found = re.match('.*"ap_enabled": "(\d+)"', html, re.S)
		if found:
			# debug("[FritzCallFBF] _okSetWlanState: ap_enabled: " + found.group(1))
			wlanState = [ found.group(1), None, None ]
			found = re.match('.*"encryption": "(\d+)"', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetWlanState: encryption: " + found.group(1))
				wlanState[1] = found.group(1)
			found = re.match('.*"active_stations": "(\d+)"', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetWlanState: active_stations: " + found.group(1))
				wlanState[2] = found.group(1)
			(boxInfo, upTime, ipAddress, dummy, dslState, tamActive, dectActive, faxActive, rufumlActive) = self.info
			self.info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
			debug("[FritzCallFBF] _okSetWlanState info: " + str(self.info))
		if callback:
			callback(self.info)

	def _okSetDslState(self, callback, html):
		# debug("[FritzCallFBF] _okSetDslState: " + html)
		found = re.match('.*"dsl_carrier_state": "(\d+)"', html, re.S)
		if found:
			# debug("[FritzCallFBF] _okSetDslState: dsl_carrier_state: " + found.group(1))
			dslState = [ found.group(1), "" ]
			found = re.match('.*"dsl_ds_nrate": "(\d+)"', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetDslState: dsl_ds_nrate: " + found.group(1))
				dslState[1] = found.group(1)
			found = re.match('.*"dsl_us_nrate": "(\d+)"', html, re.S)
			if found:
				# debug("[FritzCallFBF] _okSetDslState: dsl_us_nrate: " + found.group(1))
				dslState[1] = dslState[1] + '/' + found.group(1)
			(boxInfo, upTime, ipAddress, wlanState, dummy, tamActive, dectActive, faxActive, rufumlActive) = self.info
			self.info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
			debug("[FritzCallFBF] _okSetDslState info: " + str(self.info))
		if callback:
			callback(self.info)

	def _errorGetInfo(self, error):
		debug("[FritzCallFBF] _errorGetInfo: %s" % (error))
		text = _("FRITZ!Box - Error getting status: %s") % error.getErrorMessage()
		self._notify(text)
		# linkP = open("/tmp/FritzCall_errorGetInfo.htm", "w")
		# linkP.write(error)
		# linkP.close()

	def reset(self):
		self._login(self._reset)

	def _reset(self, html):
		# POSTDATA=getpage=../html/reboot.html&errorpage=../html/de/menus/menu2.html&var:lang=de&var:pagename=home&var:errorpagename=home&var:menu=home&var:pagemaster=&time:settings/time=1242207340%2C-120&var:tabReset=0&logic:command/reboot=../gateway/commands/saveconfig.html
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorReset('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorReset('Login: ' + html[start, html.find('</p>', start)])
				return
		if self._callScreen:
			self._callScreen.close()
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/reboot.html',
			'var:lang':'de',
			'var:pagename':'reset',
			'var:menu':'system',
			'logic:command/reboot':'../gateway/commands/saveconfig.html',
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] _reset url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms)

	def _okReset(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF] _okReset")

	def _errorReset(self, error):
		debug("[FritzCallFBF] _errorReset: %s" % (error))
		text = _("FRITZ!Box - Error resetting: %s") % error.getErrorMessage()
		self._notify(text)

	def readBlacklist(self):
		self._login(self._readBlacklist)
		
	def _readBlacklist(self, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorBlacklist('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorBlacklist('Login: ' + html[start, html.find('</p>', start)])
				return
		# http://fritz.box/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:lang=de&var:menu=fon&var:pagename=sperre
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:lang':'de',
			'var:pagename':'sperre',
			'var:menu':'fon',
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] _readBlacklist url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okBlacklist).addErrback(self._errorBlacklist)

	def _okBlacklist(self, html):
		debug("[FritzCallFBF] _okBlacklist")
		entries = re.compile('<script type="text/javascript">document.write\(Tr(Out|In)\("\d+", "(\d+)", "\w*"\)\);</script>', re.S).finditer(html)
		self.blacklist = ([], [])
		for entry in entries:
			if entry.group(1) == "In":
				self.blacklist[0].append(entry.group(2))
			else:
				self.blacklist[1].append(entry.group(2))
		debug("[FritzCallFBF] _okBlacklist: %s" % repr(self.blacklist))

	def _errorBlacklist(self, error):
		debug("[FritzCallFBF] _errorBlacklist: %s" % (error))
		text = _("FRITZ!Box - Error getting blacklist: %s") % error.getErrorMessage()
		self._notify(text)

#===============================================================================
#	def hangup(self):
#		''' hangup call on port; not used for now '''
#		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
#		parms = urlencode({
#			'id':'uiPostForm',
#			'name':'uiPostForm',
#			'login:command/password': config.plugins.FritzCall.password.value,
#			'telcfg:settings/UseClickToDial':'1',
#			'telcfg:settings/DialPort':config.plugins.FritzCall.extension.value,
#			'telcfg:command/Hangup':'',
#			'sid':self._md5Sid
#			})
#		debug("[FritzCallFBF] hangup url: '" + url + "' parms: '" + parms + "'")
#		getPage(url,
#			method="POST",
#			headers={
#					'Content-Type': "application/x-www-form-urlencoded",
#					'Content-Length': str(len(parms))},
#			postdata=parms)
#===============================================================================

import xml.etree.ElementTree as ET
import StringIO, csv

class FritzCallFBF_05_50:
	def __init__(self):
		debug("[FritzCallFBF_05_50] __init__")
		self._callScreen = None
		self._md5LoginTimestamp = None
		self._md5Sid = '0000000000000000'
		self._callTimestamp = 0
		self._callList = []
		self._callType = config.plugins.FritzCall.fbfCalls.value
		self._phoneBookID = '0'
		self._loginCallbacks = []
		self.blacklist = ([], [])
		self.info = None # (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive)
		self.phonebook = None
		self.getInfo(None)
		# self.readBlacklist() now in getInfo
		self.phonebooksFBF = []

	def _notify(self, text):
		debug("[FritzCallFBF_05_50] notify: " + text)
		self._md5LoginTimestamp = None
		if self._callScreen:
			debug("[FritzCallFBF_05_50] notify: try to close callScreen")
			self._callScreen.close()
			self._callScreen = None
		Notifications.AddNotification(MessageBox, text, type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
			
	def _login(self, callback=None):
		debug("[FritzCallFBF_05_50] _login: " + time.ctime())
		if callback:
			debug("[FritzCallFBF_05_50] _login: add callback " + callback.__name__)
			if self._loginCallbacks:
				# if login in process just add callback to _loginCallbacks
				self._loginCallbacks.append(callback)
				debug("[FritzCallFBF_05_50] _login: login in progress: leave")
				return
			else:
				self._loginCallbacks.append(callback)

		if self._callScreen:
			self._callScreen.updateStatus(_("login"))
		if self._md5LoginTimestamp and ((time.time() - self._md5LoginTimestamp) < float(9.5*60)) and self._md5Sid != '0000000000000000': # new login after 9.5 minutes inactivity 
			debug("[FritzCallFBF_05_50] _login: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
			self._md5LoginTimestamp = time.time()
			for callback in self._loginCallbacks:
				debug("[FritzCallFBF_05_50] _login: calling " + callback.__name__)
				callback(None)
			self._loginCallbacks = []
		else:
			debug("[FritzCallFBF_05_50] _login: not logged in or outdated login")
			# http://fritz.box/login_lua.xml
			url = "http://%s/login_sid.lua" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF_05_50] _login: '" + url)
			getPage(url,
				method="GET",
				headers={'Content-Type': "application/x-www-form-urlencoded"}
				).addCallback(self._md5Login).addErrback(self._errorLogin)

	def _md5Login(self, sidXml):
		def buildResponse(challenge, text):
			debug("[FritzCallFBF_05_50] _md5Login7buildResponse: challenge: " + challenge + ' text: ' + __(text))
			text = (challenge + '-' + text).decode('utf-8','ignore').encode('utf-16-le')
			for i in range(len(text)):
				if ord(text[i]) > 255:
					text[i] = '.'
			md5 = hashlib.md5()
			md5.update(text)
			debug("[FritzCallFBF_05_50] md5Login/buildResponse: " + md5.hexdigest())
			return challenge + '-' + md5.hexdigest()

		#=======================================================================
		# linkP = open("/tmp/FritzDebug_sid.xml", "w")
		# linkP.write(sidXml)
		# linkP.close()
		#=======================================================================

		debug("[FritzCallFBF_05_50] _md5Login")
		sidX = ET.fromstring(sidXml)
	#===========================================================================
	#	self._md5Sid = sidX.find("SID").text
	#	if self._md5Sid:
	#		debug("[FritzCallFBF_05_50] _md5Login: SID "+ self._md5Sid)
	#	else:
	#		debug("[FritzCallFBF_05_50] _md5Login: no sid! That must be an old firmware.")
	#		self._notify(_("FRITZ!Box - Error logging in\n\n") + _("wrong firmware version?"))
	#		return
	# 
	#	if self._md5Sid != "0000000000000000":
	#		debug("[FritzCallFBF_05_50] _md5Login: SID "+ self._md5Sid)
	#		for callback in self._loginCallbacks:
	#			debug("[FritzCallFBF_05_50] _md5Login: calling " + callback.__name__)
	#			callback(None)
	#		self._loginCallbacks = []
	#		return
	#===========================================================================

		challenge = sidX.find("Challenge").text
		if challenge:
			debug("[FritzCallFBF_05_50] _md5Login: challenge " + challenge)
		else:
			debug("[FritzCallFBF_05_50] _md5Login: login necessary and no challenge! That is terribly wrong.")

		# TODO: check validity of username?
		parms = urlencode({
						'username': config.plugins.FritzCall.username.value,
						'response': buildResponse(challenge, config.plugins.FritzCall.password.value),
						})
		url = "http://%s/login_sid.lua" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF_05_50] _md5Login: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._gotPageLogin).addErrback(self._errorLogin)

	def _gotPageLogin(self, sidXml):
		if self._callScreen:
			self._callScreen.updateStatus(_("login verification"))

		#=======================================================================
		# linkP = open("/tmp/sid.xml", "w")
		# linkP.write(sidXml)
		# linkP.close()
		#=======================================================================

		sidX = ET.fromstring(sidXml)
		self._md5Sid = sidX.find("SID").text
		if self._md5Sid and self._md5Sid != "0000000000000000":
			debug("[FritzCallFBF_05_50] _gotPageLogin: found sid: " + self._md5Sid)
		else:
			self._notify(_("FRITZ!Box - Error logging in\n\n") + _("wrong user or password?"))
			return

		if self._callScreen:
			self._callScreen.updateStatus(_("login ok"))

		debug("[FritzCallFBF_05_50] _gotPageLogin: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
		self._md5LoginTimestamp = time.time()

		for callback in self._loginCallbacks:
			debug("[FritzCallFBF_05_50] _gotPageLogin: calling " + callback.__name__)
			callback(None)
		self._loginCallbacks = []

	def _errorLogin(self, error):
		global fritzbox
		if type(error).__name__ == "str":
			text = error
		else:
			text = error.getErrorMessage()
		text = _("FRITZ!Box - Error logging in: %s\nDisabling plugin.") % text
		# config.plugins.FritzCall.enable.value = False
		fritzbox = None
		debug("[FritzCallFBF_05_50] _errorLogin: %s" % (error))
		self._notify(text)

	def _logout(self):
		if self._md5LoginTimestamp:
			self._md5LoginTimestamp = None
			parms = urlencode({
							'sid':self._md5Sid,
							'logout':'bye bye Fritz'
							})
			url = "http://%s/login_sid.lua" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF_05_50] logout: " + url + "?" + parms)
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addErrback(self._errorLogout)

	def _errorLogout(self, error):
		debug("[FritzCallFBF_05_50] _errorLogout: %s" % (error))
		text = _("FRITZ!Box - Error logging out: %s") % error.getErrorMessage()
		self._notify(text)

	def loadFritzBoxPhonebook(self, phonebook):
		self.phonebook = phonebook
		self._login(self._selectFritzBoxPhonebook)

	def _selectFritzBoxPhonebook(self, html=None):
		# TODO: error check...
		# look for phonebook called dreambox or Dreambox
		parms = urlencode({
						'sid':self._md5Sid,
						})
		url = "http://%s/fon_num/fonbook_select.lua" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF_05_50] _selectPhonebook: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._loadFritzBoxPhonebook).addErrback(self._errorLoad)

	def _loadFritzBoxPhonebook(self, html):
		# Firmware 05.27 onwards
		# look for phonebook called [dD]reambox and get bookid
		found = re.match('.*<label for="uiBookid:([\d]+)">' + config.plugins.FritzCall.fritzphonebookName.value, html, re.S)
		if found:
			bookid = found.group(1)
		else:
			bookid = 1
		debug("[FritzCallFBF_05_50] _loadFritzBoxPhonebook: phonebook %s" % (bookid))

		# http://192.168.178.1/fon_num/fonbook_list.lua?sid=2faec13b0000f3a2
		parms = urlencode({
						'bookid':bookid,
						'sid':self._md5Sid,
						})
		url = "http://%s/fon_num/fonbook_list.lua" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF_05_50] _loadFritzBoxPhonebookNew: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._parseFritzBoxPhonebook).addErrback(self._errorLoad)

	def _parseFritzBoxPhonebook(self, html):
		debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook")
		# first, let us get the charset
		found = re.match('.*<meta http-equiv=content-type content="text/html; charset=([^"]*)">', html, re.S)
		if found:
			charset = found.group(1)
			debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: found charset: " + charset)
			if charset != 'utf-8':
				html = html2unicode(html.replace(chr(0xf6),'').decode(charset)).encode('utf-8')
		else: # this is kind of emergency conversion...
			try:
				debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: try charset utf-8")
				charset = 'utf-8'
				html = html2unicode(html.decode('utf-8')).encode('utf-8') # this looks silly, but has to be
			except UnicodeDecodeError:
				debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: try charset iso-8859-1")
				charset = 'iso-8859-1'
				html = html2unicode(html.decode('iso-8859-1')).encode('utf-8') # this looks silly, but has to be

		# cleanout hrefs
		html = re.sub("<a href[^>]*>", "", html)
		html = re.sub("</a>", "", html)

		#=======================================================================
		# linkP = open("/tmp/FritzCall_Phonebook.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================

		if html.find('class="zebra_reverse"') != -1:
			debug("[FritzCallFBF_05_50] Found new 7390 firmware")
			entrymask = re.compile('<td class="tname" title="([^"]*)">[^<]*</td><td class="tnum">([^<]+(?:<br>[^<]+)*)</td><td class="ttype">([^<]+(?:<br>[^<]+)*)</td><td class="tcode">([^<]*(?:<br>[^<]*)*)</td><td class="tvanity">([^<]*(?:<br>[^<]*)*)</td>', re.S)
			entries = entrymask.finditer(html)
			for found in entries:
				# debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: processing entry for '''%s'''" % repr(found.groups()))
				name = html2unicode(re.sub(",", "", found.group(1)))
				thisnumbers = found.group(2).split("<br>")
				thistypes = found.group(3).split("<br>")
				thiscodes = found.group(4).split("<br>")
				thisvanitys = found.group(5).split("<br>")
				for i in range(len(thisnumbers)):
					thisnumber = cleanNumber(thisnumbers[i])
					if self.phonebook.phonebook.has_key(thisnumber):
						debug("[FritzCallFBF_05_50] Ignoring '''%s''' with '''%s'''" % (name, __(thisnumber)))
						continue

					if not thisnumbers[i]:
						debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: Ignoring entry with empty number for '''%s'''" % (__(name)))
						continue
					else:
						thisname = name
						if config.plugins.FritzCall.showType.value and thistypes[i]:
							thisname = thisname + " (" + thistypes[i] + ")"
						if config.plugins.FritzCall.showShortcut.value and thiscodes[i]:
							thisname = thisname + ", " + _("Shortcut") + ": " + thiscodes[i]
						if config.plugins.FritzCall.showVanity.value and thisvanitys[i]:
							thisname = thisname + ", " + _("Vanity") + ": " + thisvanitys[i]
	
						debug("[FritzCallFBF_05_50] _parseFritzBoxPhonebook: Adding '''%s''' with '''%s'''" % (__(thisname.strip()), __(thisnumber, False)))
						# Beware: strings in phonebook.phonebook have to be in utf-8!
						self.phonebook.phonebook[thisnumber] = thisname
		else:
			self._notify(_("Could not parse FRITZ!Box Phonebook entry"))

	def _errorLoad(self, error):
		debug("[FritzCallFBF_05_50] _errorLoad: %s" % (error))
		text = _("FRITZ!Box - Could not load phonebook: %s") % error.getErrorMessage()
		self._notify(text)

	def getCalls(self, callScreen, callback, callType):
		#
		# FW 05.27 onwards
		#
		debug("[FritzCallFBF] getCalls")
		self._callScreen = callScreen
		self._callType = callType
		self._login(lambda x:self._getCalls(callback, x))

	def _getCalls(self, callback, html):
		debug("[FritzCallFBF_05_50] _getCalls")
		if self._callScreen:
			self._callScreen.updateStatus(_("preparing"))
		# besser csv mit: https://fritz.box/fon_num/foncalls_list.lua?sid=dea373c2d0257a41&csv=
		parms = urlencode({'sid':self._md5Sid, 'csv':''})
		url = "http://%s/fon_num/foncalls_list.lua?%s" % (config.plugins.FritzCall.hostname.value, parms)
		getPage(url).addCallback(lambda x:self._gotPageCalls(callback, x)).addErrback(self._errorCalls)

	def _gotPageCalls(self, callback, csvString=""):

		debug("[FritzCallFBF_05_50] _gotPageCalls")
		if self._callScreen:
			self._callScreen.updateStatus(_("finishing"))

		callListL = []
		if config.plugins.FritzCall.filter.value and config.plugins.FritzCall.filterCallList.value:
			filtermsns = map(lambda x: x.strip(), config.plugins.FritzCall.filtermsn.value.split(","))
			# TODO: scramble filtermsns
			debug("[FritzCallFBF_05_50] _gotPageCalls: filtermsns %s" % (repr(map(__, filtermsns))))
		else:
			filtermsns = None

		#=======================================================================
		# linkP = open("/tmp/FritzCalls.csv", "w")
		# linkP.write(csvString)
		# linkP.close()
		#=======================================================================

		# 0: direct; 1: date; 2: Name; 3: Nummer; 4: Nebenstelle; 5: Eigene Rufnumme; 6: Dauer
		calls = csv.reader(StringIO.StringIO(csvString), delimiter=';')
		calls.next() # skip sep
		for call in calls:
			if len(call) != 7:
				debug("[FritzCallFBF_05_50] _gotPageCalls: skip %s len: %s" %(repr(call), str(len(call))))
				continue
			direct = call[0]
			if direct == '1':
				direct = FBF_IN_CALLS
			elif direct == '4':
				direct = FBF_OUT_CALLS
			elif direct == '2':
				direct = FBF_MISSED_CALLS
			if self._callType != '.' and self._callType != direct:
				continue

			date = call[1]
			length = call[6]
			number = stripCbCPrefix(call[3], config.plugins.FritzCall.country.value)
			if config.plugins.FritzCall.prefix.value and number and number[0] != '0':		# should only happen for outgoing
				number = config.plugins.FritzCall.prefix.value + number
			# debug("[FritzCallFBF_05_50] _gotPageCalls: number: " + number)

			found = re.match("\d+ \((\d+)\)", call[2])
			if found:
				remote = resolveNumber(number, resolveNumber(found.group(1), None, self.phonebook), self.phonebook)
			else:
				remote = resolveNumber(number, call[2], self.phonebook)
			# debug("[FritzCallFBF_05_50] _gotPageCalls: remote. " + remote)

			here = call[5]
			start = here.find('Internet: ')
			if start != -1:
				start += len('Internet: ')
				here = here[start:]

			if filtermsns and here not in filtermsns:
				# debug("[FritzCallFBF_05_50] _gotPageCalls: skip %s" % (here))
				continue

			here = resolveNumber(here, call[4], self.phonebook)
			# debug("[FritzCallFBF_05_50] _gotPageCalls: here: " + here)

			debug("[FritzCallFBF_05_50] _gotPageCalls: append: %s" % repr((__(number, False), date, direct, __(remote), length, __(here))))
			# debug("[FritzCallFBF_05_50] _gotPageCalls: append: %s" % repr((number, date, direct, remote, length, here)))
			callListL.append((number, date, direct, remote, length, here))

		if callback:
			# debug("[FritzCallFBF_05_50] _gotPageCalls call callback with\n" + text
			callback(callListL)
		self._callScreen = None

	def _errorCalls(self, error):
		debug("[FritzCallFBF_05_50] _errorCalls: %s" % (error))
		text = _("FRITZ!Box - Could not load calls: %s") % error.getErrorMessage()
		self._notify(text)

	def dial(self, number):
		''' initiate a call to number '''
		self._login(lambda x: self._dial(number, x))
		
	def _dial(self, number, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorDial('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorDial('Login: ' + html[start, html.find('</p>', start)])
				return
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:pagename':'fonbuch',
			'var:menu':'home',
			'telcfg:settings/UseClickToDial':'1',
			'telcfg:settings/DialPort':config.plugins.FritzCall.extension.value,
			'telcfg:command/Dial':number,
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_50] dial url: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okDial).addErrback(self._errorDial)

	def _okDial(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_50] okDial")
		if html:
			found = re.match('.*<p class="ErrorMsg">([^<]*)</p>', html, re.S)
			if found:
				self._notify(found.group(1))
				return

	def _errorDial(self, error):
		debug("[FritzCallFBF_05_50] errorDial: $s" % error)
		text = _("FRITZ!Box - Dialling failed: %s") % error.getErrorMessage()
		self._notify(text)

	def changeWLAN(self, statusWLAN):
		''' get status info from FBF '''
		debug("[FritzCallFBF_05_50] changeWLAN start")
		#=======================================================================
		# Notifications.AddNotification(MessageBox, _("not yet implemented"), type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
		# return
		#=======================================================================

		if not statusWLAN or (statusWLAN != '1' and statusWLAN != '0'):
			return
		self._login(lambda x: self._changeWLAN(statusWLAN, x))
		
	def _changeWLAN(self, statusWLAN, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorChangeWLAN('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorChangeWLAN('Login: ' + html[start, html.find('</p>', start)])
				return

		if statusWLAN == '0':
			parms = urlencode({
				'sid':self._md5Sid,
				'apply':'',
				'cancel':'',
				'btn_refresh':''
				})
		else:
			parms = urlencode({
				'sid':self._md5Sid,
				'active':'on',
				'active_24':'on',
				'active_5':'on',
				'hidden_ssid':'on',
				'apply':'',
				'cancel':'',
				'btn_refresh':''
				})

		url = "http://%s//wlan/wlan_settings.lua" % config.plugins.FritzCall.hostname.value
		debug("[FritzCallFBF_05_50] changeWLAN url: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded"},
			postdata=parms).addCallback(self._okChangeWLAN).addErrback(self._errorChangeWLAN)

	def _okChangeWLAN(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_50] _okChangeWLAN")
		if html:
			found = re.match('.*<p class="ErrorMsg">([^<]*)</p>', html, re.S)
			if found:
				self._notify(found.group(1))
				return

	def _errorChangeWLAN(self, error):
		debug("[FritzCallFBF_05_50] _errorChangeWLAN: $s" % error)
		text = _("FRITZ!Box - Failed changing WLAN: %s") % error.getErrorMessage()
		self._notify(text)

	def changeMailbox(self, whichMailbox):
		''' switch mailbox on/off '''
		debug("[FritzCallFBF_05_50] changeMailbox start: " + str(whichMailbox))
		Notifications.AddNotification(MessageBox, _("not yet implemented"), type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)

	def getInfo(self, callback):
		''' get status info from FBF '''
		debug("[FritzCallFBF_05_50] getInfo")
		self._login(lambda x:self._getInfo(callback, x))
		
	def _getInfo(self, callback, html):
		debug("[FritzCallFBF_05_50] _getInfo: verify login")
		if html:
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorGetInfo('Login: ' + html[start, html.find('</p>', start)])
				return

		self._readBlacklist()

		url = "http://%s/home/home.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_50] _getInfo url: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(lambda x:self._okGetInfo(callback,x)).addErrback(self._errorGetInfo)

	def _okGetInfo(self, callback, html):

		debug("[FritzCallFBF_05_50] _okGetInfo")

		#=======================================================================
		# linkP = open("/tmp/FritzCallInfo.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================

		(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = (None, None, None, None, None, None, None, None, None)

		found = re.match('.*<table id="tProdukt" class="tborder"> <tr> <td style="[^"]*" >([^<]*)</td> <td style="[^"]*" class="td_right">([^<]*)<a target="[^"]*" onclick="[^"]*" href="[^"]*">([^<]*)</a></td> ', html, re.S)
		if found:
			boxInfo = found.group(1) + ', ' + found.group(2) + found.group(3)
			boxInfo = boxInfo.replace('&nbsp;',' ')
			debug("[FritzCallFBF_05_50] _okGetInfo Boxinfo: " + boxInfo)

		found = re.match('.*<div id=\'ipv4_info\'><span class="[^"]*">verbunden seit ([^<]*)</span>', html, re.S)
		if found:
			upTime = found.group(1)
			debug("[FritzCallFBF_05_50] _okGetInfo upTime: " + upTime)

		found = re.match('.*IP-Adresse: ([^<]*)</span>', html, re.S)
		if found:
			ipAddress = found.group(1)
			debug("[FritzCallFBF_05_50] _okGetInfo ipAddress: " + ipAddress)

		found = re.match('.*<tr id="uiTrDsl"><td class="(led_gray|led_green|led_red)">', html, re.S)
		if found:
			if found.group(1) == "led_green":
				dslState = ['5', None, None]
				found = re.match('.*<a href="[^"]*">DSL</a></td><td >bereit, ([^<]*)<img src=\'[^\']*\' height=\'[^\']*\'>&nbsp;([^<]*)<img src=\'[^\']*\' height=\'[^\']*\'></td></tr>', html, re.S)
				if found:
					dslState[1] = found.group(1) + " / " + found.group(2)
			else:
				dslState = ['0', None, None]
		debug("[FritzCallFBF_05_50] _okGetInfo dslState: " + repr(dslState))

		# wlanstate = [ active, encrypted, no of devices ]
		found = re.match('.*<tr id="uiTrWlan"><td class="(led_gray|led_green|led_red)"></td><td><a href="[^"]*">WLAN</a></td><td>(aus|an)(|, gesichert)</td>', html, re.S)
		if found:
			if found.group(1) == "led_green":
				if found.group(3):
					wlanState = [ '1', '1', '' ]
				else:
					wlanState = [ '1', '0', '' ]
			else:
				wlanState = [ '0', '0', '0' ]
			debug("[FritzCallFBF_05_50] _okGetInfo wlanState: " + repr(wlanState))

		#=======================================================================
		# found = re.match('.*<tr id="trTam" style=""><td><a href="[^"]*">Anrufbeantworter</a></td><td title=\'[^\']*\'>([\d]+) aktiv([^<]*)</td></tr>', html, re.S)
		# if found:
		#	# found.group(2) could be ', neue Nachrichten vorhanden'; ignore for now
		#	tamActive = [ found.group(1), False, False, False, False, False]
		# debug("[FritzCallFBF_05_50] _okGetInfo tamActive: " + repr(tamActive))
		#=======================================================================

		found = re.match('.*<tr id="uiTrDect"><td class="(led_gray|led_green|led_red)"></td><td><a href="[^"]*">DECT</a></td><td>(?:aus|an, (ein|\d*) Schnurlostelefon)', html, re.S)
		if found:
			debug("[FritzCallFBF_05_50] _okGetInfo dectActive: " + repr(found.groups()))
			if found.group(1) == "led_green":
				dectActive = found.group(2)
				debug("[FritzCallFBF_05_50] _okGetInfo dectActive: " + repr(dectActive))

		found = re.match('.*<td>Integriertes Fax aktiv</td>', html, re.S)
		if found:
			faxActive = True
			debug("[FritzCallFBF_05_50] _okGetInfo faxActive: " + repr(faxActive))

		found = re.match('.*Rufumleitung</a></td><td>aktiv</td>', html, re.S)
		if found:
			rufumlActive = -1 # means no number available
			debug("[FritzCallFBF_05_50] _okGetInfo rufumlActive: " + repr(rufumlActive))

		info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
		debug("[FritzCallFBF_05_50] _okGetInfo info: " + str(info))
		self.info = info
		if callback:
			callback(info)

	def _errorGetInfo(self, error):
		debug("[FritzCallFBF_05_50] _errorGetInfo: %s" % (error))
		text = _("FRITZ!Box - Error getting status: %s") % error.getErrorMessage()
		self._notify(text)
		return

	def reset(self):
		self._login(self._reset)

	def _reset(self, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorReset('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorReset('Login: ' + html[start, html.find('</p>', start)])
				return

		if self._callScreen:
			self._callScreen.close()

		url = "http://%s/system/reboot.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'reboot':'',
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_50] _reset url: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded"},
			postdata=parms).addCallback(self._okReset).addErrback(self._errorReset)

		self._md5LoginTimestamp = None

	def _okReset(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_50] _okReset")
		#=======================================================================
		# linkP = open("/tmp/_okReset.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================
		if html:
			found = re.match('.*<p class="ErrorMsg">([^<]*)</p>', html, re.S)
			if found:
				self._notify(found.group(1))
				return

	def _errorReset(self, error):
		debug("[FritzCallFBF_05_50] _errorReset: %s" % (error))
		text = _("FRITZ!Box - Error resetting: %s") % error.getErrorMessage()
		self._notify(text)

	def _readBlacklist(self):
		# http://fritz.box/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:lang=de&var:menu=fon&var:pagename=sperre
		url = "http://%s/fon_num/sperre.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_50] _readBlacklist url: " + url + "?" + parms)
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okBlacklist).addErrback(self._errorBlacklist)

	def _okBlacklist(self, html):
		debug("[FritzCallFBF_05_50] _okBlacklist")
		#=======================================================================
		# linkP = open("/tmp/FritzCallBlacklist.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================
		entries = re.compile('<span title="(?:Ankommende|Ausgehende) Rufe">(Ankommende|Ausgehende) Rufe</span></nobr></td><td><nobr><span title="[\d]+">([\d]+)</span>', re.S).finditer(html)
		self.blacklist = ([], [])
		for entry in entries:
			if entry.group(1) == "Ankommende":
				self.blacklist[0].append(entry.group(2))
			else:
				self.blacklist[1].append(entry.group(2))
		debug("[FritzCallFBF_05_50] _okBlacklist: %s" % repr(self.blacklist))

	def _errorBlacklist(self, error):
		debug("[FritzCallFBF_05_50] _errorBlacklist: %s" % (error))
		text = _("FRITZ!Box - Error getting blacklist: %s") % error.getErrorMessage()
		self._notify(text)

class FritzCallFBF_05_27:
	def __init__(self):
		debug("[FritzCallFBF_05_27] __init__")
		self._callScreen = None
		self._md5LoginTimestamp = None
		self._md5Sid = '0000000000000000'
		self._callTimestamp = 0
		self._callList = []
		self._callType = config.plugins.FritzCall.fbfCalls.value
		self._phoneBookID = '0'
		self._loginCallbacks = []
		self.blacklist = ([], [])
		self.info = None # (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive)
		self.phonebook = None
		self.getInfo(None)
		# self.readBlacklist() now in getInfo
		self.phonebooksFBF = []

	def _notify(self, text):
		debug("[FritzCallFBF_05_27] notify: " + text)
		self._md5LoginTimestamp = None
		if self._callScreen:
			debug("[FritzCallFBF_05_27] notify: try to close callScreen")
			self._callScreen.close()
			self._callScreen = None
		Notifications.AddNotification(MessageBox, text, type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
			
	def _login(self, callback=None):
		debug("[FritzCallFBF_05_27] _login: " + time.ctime())
		if callback:
			debug("[FritzCallFBF_05_27] _login: add callback " + callback.__name__)
			if self._loginCallbacks:
				# if login in process just add callback to _loginCallbacks
				self._loginCallbacks.append(callback)
				debug("[FritzCallFBF_05_27] _login: login in progress: leave")
				return
			else:
				self._loginCallbacks.append(callback)

		if self._callScreen:
			self._callScreen.updateStatus(_("login"))
		if self._md5LoginTimestamp and ((time.time() - self._md5LoginTimestamp) < float(9.5*60)) and self._md5Sid != '0000000000000000': # new login after 9.5 minutes inactivity 
			debug("[FritzCallFBF_05_27] _login: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
			self._md5LoginTimestamp = time.time()
			for callback in self._loginCallbacks:
				debug("[FritzCallFBF_05_27] _login: calling " + callback.__name__)
				callback(None)
			self._loginCallbacks = []
		else:
			debug("[FritzCallFBF_05_27] _login: not logged in or outdated login")
			# http://fritz.box/cgi-bin/webcm?getpage=../html/login_sid.xml
			parms = urlencode({'getpage':'../html/login_sid.xml'})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF_05_27] _login: '" + url + "?" + parms + "'")
			getPage(url,
				method="POST",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addCallback(self._md5Login).addErrback(self._errorLogin)

	def _md5Login(self, sidXml):
		def buildResponse(challenge, text):
			debug("[FritzCallFBF_05_27] _md5Login7buildResponse: challenge: " + challenge + ' text: ' + __(text))
			text = (challenge + '-' + text).decode('utf-8','ignore').encode('utf-16-le')
			for i in range(len(text)):
				if ord(text[i]) > 255:
					text[i] = '.'
			md5 = hashlib.md5()
			md5.update(text)
			debug("[FritzCallFBF_05_27] md5Login/buildResponse: " + md5.hexdigest())
			return challenge + '-' + md5.hexdigest()

		debug("[FritzCallFBF_05_27] _md5Login")
		found = re.match('.*<SID>([^<]*)</SID>', sidXml, re.S)
		if found:
			self._md5Sid = found.group(1)
			debug("[FritzCallFBF_05_27] _md5Login: SID "+ self._md5Sid)
		else:
			debug("[FritzCallFBF_05_27] _md5Login: no sid! That must be an old firmware.")
			self._errorLogin('No sid?!?')
			return

		debug("[FritzCallFBF_05_27] _md5Login: renew timestamp: " + time.ctime(self._md5LoginTimestamp) + " time: " + time.ctime())
		self._md5LoginTimestamp = time.time()
		if sidXml.find('<iswriteaccess>0</iswriteaccess>') != -1:
			debug("[FritzCallFBF_05_27] _md5Login: logging in")
			found = re.match('.*<Challenge>([^<]*)</Challenge>', sidXml, re.S)
			if found:
				challenge = found.group(1)
				debug("[FritzCallFBF_05_27] _md5Login: challenge " + challenge)
			else:
				challenge = None
				debug("[FritzCallFBF_05_27] _md5Login: login necessary and no challenge! That is terribly wrong.")
			parms = urlencode({
							'getpage':'../html/de/menus/menu2.html', # 'var:pagename':'home', 'var:menu':'home', 
							'login:command/response': buildResponse(challenge, config.plugins.FritzCall.password.value),
							})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF_05_27] _md5Login: '" + url + "?" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addCallback(self._gotPageLogin).addErrback(self._errorLogin)
		else:
			for callback in self._loginCallbacks:
				debug("[FritzCallFBF_05_27] _md5Login: calling " + callback.__name__)
				callback(None)
			self._loginCallbacks = []

	def _gotPageLogin(self, html):
		if self._callScreen:
			self._callScreen.updateStatus(_("login verification"))
		debug("[FritzCallFBF_05_27] _gotPageLogin: verify login")
		start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
		if start != -1:
			start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
			text = _("FRITZ!Box - Error logging in\n\n") + html[start : html.find('</p>', start)]
			self._notify(text)
		else:
			if self._callScreen:
				self._callScreen.updateStatus(_("login ok"))

		found = re.match('.*<input type="hidden" name="sid" value="([^\"]*)"', html, re.S)
		if found:
			self._md5Sid = found.group(1)
			debug("[FritzCallFBF_05_27] _gotPageLogin: found sid: " + self._md5Sid)

		for callback in self._loginCallbacks:
			debug("[FritzCallFBF_05_27] _gotPageLogin: calling " + callback.__name__)
			callback(None)
		self._loginCallbacks = []

	def _errorLogin(self, error):
		global fritzbox
		debug("[FritzCallFBF_05_27] _errorLogin: %s" % (error))
		if type(error) != str:
			error =  error.getErrorMessage()
		text = _("FRITZ!Box - Error logging in: %s\nDisabling plugin.") % error
		# config.plugins.FritzCall.enable.value = False
		fritzbox = None
		self._notify(text)

	def _logout(self):
		if self._md5LoginTimestamp:
			self._md5LoginTimestamp = None
			parms = urlencode({
							'getpage':'../html/de/menus/menu2.html', # 'var:pagename':'home', 'var:menu':'home', 
							'login:command/logout':'bye bye Fritz'
							})
			url = "http://%s/cgi-bin/webcm" % (config.plugins.FritzCall.hostname.value)
			debug("[FritzCallFBF_05_27] logout: '" + url + "' parms: '" + parms + "'")
			getPage(url,
				method="POST",
				agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
				headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
						}, postdata=parms).addErrback(self._errorLogout)

	def _errorLogout(self, error):
		debug("[FritzCallFBF_05_27] _errorLogout: %s" % (error))
		text = _("FRITZ!Box - Error logging out: %s") % error.getErrorMessage()
		self._notify(text)

	def loadFritzBoxPhonebook(self, phonebook):
		self.phonebook = phonebook
		self._login(self._selectFritzBoxPhonebook)

	def _selectFritzBoxPhonebook(self, html):
		# first check for login error
		if html:
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorLoad('Login: ' + html[start, html.find('</p>', start)])
				return
		# look for phonebook called dreambox or Dreambox
		parms = urlencode({
						'sid':self._md5Sid,
						})
		url = "http://%s/fon_num/fonbook_select.lua" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF_05_27] _selectPhonebook: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._loadFritzBoxPhonebook).addErrback(self._errorLoad)

	def _loadFritzBoxPhonebook(self, html):
		# Firmware 05.27 onwards
		# look for phonebook called [dD]reambox and get bookid
		found = re.match('.*<label for="uiBookid:([\d]+)">' + config.plugins.FritzCall.fritzphonebookName.value, html, re.S)
		if found:
			bookid = found.group(1)
			debug("[FritzCallFBF_05_27] _loadFritzBoxPhonebook: found dreambox phonebook %s" % (bookid))
		else:
			bookid = 1
		# http://192.168.178.1/fon_num/fonbook_list.lua?sid=2faec13b0000f3a2
		parms = urlencode({
						'bookid':bookid,
						'sid':self._md5Sid,
						})
		url = "http://%s/fon_num/fonbook_list.lua" % (config.plugins.FritzCall.hostname.value)
		debug("[FritzCallFBF_05_27] _loadFritzBoxPhonebookNew: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={'Content-Type': "application/x-www-form-urlencoded", 'Content-Length': str(len(parms))
					}, postdata=parms).addCallback(self._parseFritzBoxPhonebook).addErrback(self._errorLoad)

	def _parseFritzBoxPhonebook(self, html):
		debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew")
		found = re.match('.*<input type="hidden" name="telcfg:settings/Phonebook/Books/Name\d+" value="' + config.plugins.FritzCall.fritzphonebookName.value +'" id="uiPostPhonebookName\d+" disabled>\s*<input type="hidden" name="telcfg:settings/Phonebook/Books/Id\d+" value="(\d+)" id="uiPostPhonebookId\d+" disabled>', html, re.S)
		if found:
			phoneBookID = found.group(1)
			debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: found dreambox phonebook with id: " + phoneBookID)
			if self._phoneBookID != phoneBookID:
				self._phoneBookID = phoneBookID
				debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: reload phonebook")
				self._loadFritzBoxPhonebook(None) # reload with dreambox phonebook
				return

		# first, let us get the charset
		found = re.match('.*<meta http-equiv=content-type content="text/html; charset=([^"]*)">', html, re.S)
		if found:
			charset = found.group(1)
			debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: found charset: " + charset)
			html = html2unicode(html.replace(chr(0xf6),'').decode(charset)).encode('utf-8')
		else: # this is kind of emergency conversion...
			try:
				debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: try charset utf-8")
				charset = 'utf-8'
				html = html2unicode(html.decode('utf-8')).encode('utf-8') # this looks silly, but has to be
			except UnicodeDecodeError:
				debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: try charset iso-8859-1")
				charset = 'iso-8859-1'
				html = html2unicode(html.decode('iso-8859-1')).encode('utf-8') # this looks silly, but has to be

		# cleanout hrefs
		html = re.sub("<a href[^>]*>", "", html)
		html = re.sub("</a>", "", html)
		#=======================================================================
		# linkP = open("/tmp/FritzCall_Phonebook.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================

		if html.find('class="zebra_reverse"') != -1:
			debug("[FritzCallFBF_05_27] Found new 7390 firmware")
			# <td class="tname">Mama</td><td class="tnum">03602191620<br>015228924783<br>03602181567</td><td class="ttype">geschäftl.<br>mobil<br>privat</td><td class="tcode"><br>**701<br></td><td class="tvanity"><br>1<br></td>
			entrymask = re.compile('<td class="tname">([^<]*)</td><td class="tnum">([^<]+(?:<br>[^<]+)*)</td><td class="ttype">([^<]+(?:<br>[^<]+)*)</td><td class="tcode">([^<]*(?:<br>[^<]*)*)</td><td class="tvanity">([^<]*(?:<br>[^<]*)*)</td>', re.S)
			entries = entrymask.finditer(html)
			for found in entries:
				# debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: processing entry for '''%s'''" % (found.group(1)))
				name = found.group(1)
				thisnumbers = found.group(2).split("<br>")
				thistypes = found.group(3).split("<br>")
				thiscodes = found.group(4).split("<br>")
				thisvanitys = found.group(5).split("<br>")
				for i in range(len(thisnumbers)):
					thisnumber = cleanNumber(thisnumbers[i])
					if self.phonebook.phonebook.has_key(thisnumber):
						debug("[FritzCallFBF_05_27] Ignoring '''%s''' with '''%s''' from FRITZ!Box Phonebook!" % (name, __(thisnumber)))
						continue

					if not thisnumbers[i]:
						debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: Ignoring entry with empty number for '''%s'''" % (name))
						continue
					else:
						thisname = name
						if config.plugins.FritzCall.showType.value and thistypes[i]:
							thisname = thisname + " (" + thistypes[i] + ")"
						if config.plugins.FritzCall.showShortcut.value and thiscodes[i]:
							thisname = thisname + ", " + _("Shortcut") + ": " + thiscodes[i]
						if config.plugins.FritzCall.showVanity.value and thisvanitys[i]:
							thisname = thisname + ", " + _("Vanity") + ": " + thisvanitys[i]
	
						debug("[FritzCallFBF_05_27] _parseFritzBoxPhonebookNew: Adding '''%s''' with '''%s''' from FRITZ!Box Phonebook!" % (thisname.strip(), thisnumber))
						# Beware: strings in phonebook.phonebook have to be in utf-8!
						self.phonebook.phonebook[thisnumber] = thisname
		else:
			self._notify(_("Could not parse FRITZ!Box Phonebook entry"))

	def _errorLoad(self, error):
		debug("[FritzCallFBF_05_27] _errorLoad: %s" % (error))
		text = _("FRITZ!Box - Could not load phonebook: %s") % error.getErrorMessage()
		self._notify(text)

	def getCalls(self, callScreen, callback, callType):
		#
		# FW 05.27 onwards
		#
		self._callScreen = callScreen
		self._callType = callType
		debug("[FritzCallFBF_05_27] _getCalls1New")
		if self._callScreen:
			self._callScreen.updateStatus(_("finishing"))
		# http://192.168.178.1/fon_num/foncalls_list.lua?sid=da78ab0797197dc7
		parms = urlencode({'sid':self._md5Sid})
		url = "http://%s/fon_num/foncalls_list.lua?%s" % (config.plugins.FritzCall.hostname.value, parms)
		getPage(url).addCallback(lambda x:self._gotPageCalls(callback, x)).addErrback(self._errorCalls)

	def _gotPageCalls(self, callback, html=""):

		debug("[FritzCallFBF_05_27] _gotPageCalls")
		if self._callScreen:
			self._callScreen.updateStatus(_("preparing"))

		callListL = []
		if config.plugins.FritzCall.filter.value and config.plugins.FritzCall.filterCallList.value:
			filtermsns = map(lambda x: x.strip(), config.plugins.FritzCall.filtermsn.value.split(","))
			# TODO: scramble filtermsns
			debug("[FritzCallFBF_05_27] _gotPageCalls: filtermsns %s" % (repr(filtermsns)))

		#=======================================================================
		# linkP = open("/tmp/FritzCall_Calllist.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================

		# 1: direct; 2: date; 3: Rufnummer; 4: Name; 5: Nebenstelle; 6: Eigene Rufnumme lang; 7: Eigene Rufnummer; 8: Dauer
		entrymask = re.compile('<td class="([^"]*)" title="[^"]*"></td>\s*<td>([^<]*)</td>\s*<td(?: title="[^\d]*)?([\d]*)(?:[">]+)?(?:<a href=[^>]*>)?([^<]*)(?:</a>)?</td>\s*<td>([^<]*)</td>\s*<td title="([^"]*)">([\d]*)</td>\s*<td>([^<]*)</td>', re.S)
		entries = entrymask.finditer(html)
		for found in entries:
			if found.group(1) == "call_in":
				direct = FBF_IN_CALLS
			elif found.group(1) == "call_out":
				direct = FBF_OUT_CALLS
			elif found.group(1) == "call_in_fail":
				direct = FBF_MISSED_CALLS
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: direct: " + direct)
			if direct != self._callType and "." != self._callType:
				continue

			date = found.group(2)
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: date: " + date)
			length = found.group(8)
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: len: " + length)
			remote = found.group(4)
			if config.plugins.FritzCall.phonebook.value:
				if remote and not remote.isdigit():
					remote = resolveNumber(found.group(3), remote + " (FBF)", self.phonebook)
				else:
					remote = resolveNumber(found.group(3), "", self.phonebook)
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: remote. " + remote)
			here = found.group(7)
			#===================================================================
			# start = here.find('Internet: ')
			# if start != -1:
			#	start += len('Internet: ')
			#	here = here[start:]
			# else:
			#	here = line[5]
			#===================================================================
			if config.plugins.FritzCall.filter.value and config.plugins.FritzCall.filterCallList.value:
				# debug("[FritzCallFBF_05_27] _gotPageCalls: check %s" % (here))
				if here not in filtermsns:
					# debug("[FritzCallFBF_05_27] _gotPageCalls: skip %s" % (here))
					continue
			here = resolveNumber(here, found.group(6), self.phonebook)
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: here: " + here)

			number = stripCbCPrefix(found.group(3), config.plugins.FritzCall.country.value)
			if config.plugins.FritzCall.prefix.value and number and number[0] != '0':		# should only happen for outgoing
				number = config.plugins.FritzCall.prefix.value + number
			# debug("[FritzCallFBF_05_27] _gotPageCallsNew: number: " + number)
			debug("[FritzCallFBF_05_27] _gotPageCallsNew: append: %s" % repr((number, date, direct, remote, length, here)) )
			callListL.append((number, date, direct, remote, length, here))

		if callback:
			# debug("[FritzCallFBF_05_27] _gotPageCalls call callback with\n" + text
			callback(callListL)
		self._callScreen = None

	def _errorCalls(self, error):
		debug("[FritzCallFBF_05_27] _errorCalls: %s" % (error))
		text = _("FRITZ!Box - Could not load calls: %s") % error.getErrorMessage()
		self._notify(text)

	def dial(self, number):
		''' initiate a call to number '''
		self._login(lambda x: self._dial(number, x))
		
	def _dial(self, number, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorDial('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorDial('Login: ' + html[start, html.find('</p>', start)])
				return
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/de/menus/menu2.html',
			'var:pagename':'fonbuch',
			'var:menu':'home',
			'telcfg:settings/UseClickToDial':'1',
			'telcfg:settings/DialPort':config.plugins.FritzCall.extension.value,
			'telcfg:command/Dial':number,
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_27] dial url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okDial).addErrback(self._errorDial)

	def _okDial(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_27] okDial")

	def _errorDial(self, error):
		debug("[FritzCallFBF_05_27] errorDial: $s" % error)
		text = _("FRITZ!Box - Dialling failed: %s") % error.getErrorMessage()
		self._notify(text)

	def changeWLAN(self, statusWLAN):
		''' get status info from FBF '''
		debug("[FritzCallFBF_05_27] changeWLAN start")
		Notifications.AddNotification(MessageBox, _("not yet implemented"), type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)
		return

		if not statusWLAN or (statusWLAN != '1' and statusWLAN != '0'):
			return
		self._login(lambda x: self._changeWLAN(statusWLAN, x))
		
	def _changeWLAN(self, statusWLAN, html):
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorChangeWLAN('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorChangeWLAN('Login: ' + html[start, html.find('</p>', start)])
				return

		if statusWLAN == '0':
			statusWLAN = 'off'
		else:
			statusWLAN = 'off'

		url = "http://%s//wlan/wlan_settings.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'active':str(statusWLAN),
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF] changeWLAN url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okChangeWLAN).addErrback(self._errorChangeWLAN)

	def _okChangeWLAN(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF] _okChangeWLAN")

	def _errorChangeWLAN(self, error):
		debug("[FritzCallFBF] _errorChangeWLAN: $s" % error)
		text = _("FRITZ!Box - Failed changing WLAN: %s") % error.getErrorMessage()
		self._notify(text)

	def changeMailbox(self, whichMailbox):
		''' switch mailbox on/off '''
		debug("[FritzCallFBF_05_27] changeMailbox start: " + str(whichMailbox))
		Notifications.AddNotification(MessageBox, _("not yet implemented"), type=MessageBox.TYPE_ERROR, timeout=config.plugins.FritzCall.timeout.value)

	def _changeMailbox(self, whichMailbox, html):
		return

	def _okChangeMailbox(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_27] _okChangeMailbox")

	def _errorChangeMailbox(self, error):
		debug("[FritzCallFBF_05_27] _errorChangeMailbox: $s" % error)
		text = _("FRITZ!Box - Failed changing Mailbox: %s") % error.getErrorMessage()
		self._notify(text)

	def getInfo(self, callback):
		''' get status info from FBF '''
		debug("[FritzCallFBF_05_27] getInfo")
		self._login(lambda x:self._getInfo(callback, x))
		
	def _getInfo(self, callback, html):
		debug("[FritzCallFBF_05_27] _getInfo: verify login")
		if html:
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorGetInfo('Login: ' + html[start, html.find('</p>', start)])
				return

		self._readBlacklist()

		url = "http://%s/home/home.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_27] _getInfo url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(lambda x:self._okGetInfo(callback,x)).addErrback(self._errorGetInfo)

	def _okGetInfo(self, callback, html):

		debug("[FritzCallFBF_05_27] _okGetInfo")

		#=======================================================================
		# linkP = open("/tmp/FritzCallInfo.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================

		if self.info:
			(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = self.info
		else:
			(boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive) = (None, None, None, None, None, None, None, None, None)

		found = re.match('.*<table id="tProdukt" class="tborder"> <tr> <td style="[^"]*" >([^<]*)</td> <td style="[^"]*" class="td_right">([^<]*)<a target="[^"]*" onclick="[^"]*" href="[^"]*">([^<]*)</a></td> ', html, re.S)
		if found:
			boxInfo = found.group(1) + ', ' + found.group(2) + found.group(3)
			boxInfo = boxInfo.replace('&nbsp;',' ')
			debug("[FritzCallFBF_05_27] _okGetInfo Boxinfo: " + boxInfo)

		found = re.match('.*<div id=\'ipv4_info\'><span class="[^"]*">verbunden seit ([^<]*)</span>', html, re.S)
		if found:
			upTime = found.group(1)
			debug("[FritzCallFBF_05_27] _okGetInfo upTime: " + upTime)

		found = re.match('.*IP-Adresse: ([^<]*)</span>', html, re.S)
		if found:
			ipAddress = found.group(1)
			debug("[FritzCallFBF_05_27] _okGetInfo ipAddress: " + ipAddress)

		# wlanstate = [ active, encrypted, no of devices ]
		found = re.match('.*<tr id="uiTrWlan"><td class="(led_gray|led_green|led_red)"></td><td><a href="[^"]*">WLAN</a></td><td>(aus|an)(|, gesichert)</td>', html, re.S)
		if found:
			if found.group(1) == "led_green":
				if found.group(2):
					wlanState = [ '1', '1', '' ]
				else:
					wlanState = [ '1', '0', '' ]
			else:
				wlanState = [ '0', '0', '0' ]
			debug("[FritzCallFBF_05_27] _okGetInfo wlanState: " + repr(wlanState))

		found = re.match('.*<tr id="uiTrDsl"><td class="(led_gray|led_green|led_red)">', html, re.S)
		if found:
			if found.group(1) == "led_green":
				dslState = ['5', None, None]
				found = re.match('.*<a href="[^"]*">DSL</a></td><td >bereit, ([^<]*)<img src=\'[^\']*\' height=\'[^\']*\'>&nbsp;([^<]*)<img src=\'[^\']*\' height=\'[^\']*\'></td></tr>', html, re.S)
				if found:
					dslState[1] = found.group(1) + "/" + found.group(2)
			else:
				dslState = ['0', None, None]
		debug("[FritzCallFBF_05_27] _okGetInfo dslState: " + repr(dslState))

		found = re.match('.*<tr id="trTam" style=""><td><a href="[^"]*">Anrufbeantworter</a></td><td title=\'[^\']*\'>([\d]+) aktiv([^<]*)</td></tr>', html, re.S)
		if found:
			# found.group(2) could be ', neue Nachrichten vorhanden'; ignore for now
			tamActive = [ found.group(1), False, False, False, False, False]
		debug("[FritzCallFBF_05_27] _okGetInfo tamActive: " + repr(tamActive))

		found = re.match('.*<tr id="uiTrDect"><td class="led_green"></td><td><a href="[^"]*">DECT</a></td><td>an, (ein|\d*) Schnurlostelefon', html, re.S)
		if found:
			dectActive = found.group(1)
		debug("[FritzCallFBF_05_27] _okGetInfo dectActive: " + repr(dectActive))

		found = re.match('.*<td>Integriertes Fax aktiv</td>', html, re.S)
		if found:
			faxActive = True
		debug("[FritzCallFBF_05_27] _okGetInfo faxActive: " + repr(faxActive))

		found = re.match('.* <tr style=""><td><a href="[^"]*">Rufumleitung</a></td><td>deaktiviert</td></tr>', html, re.S)
		if found:
			rufumlActive = False
		else:
			rufumlActive = True
		debug("[FritzCallFBF_05_27] _okGetInfo rufumlActive: " + repr(rufumlActive))

		info = (boxInfo, upTime, ipAddress, wlanState, dslState, tamActive, dectActive, faxActive, rufumlActive)
		debug("[FritzCallFBF_05_27] _okGetInfo info: " + str(info))
		self.info = info
		if callback:
			callback(info)

	def _okSetDect(self, callback, html):
		return
	
	def _okSetConInfo(self, callback, html):
		return

	def _okSetWlanState(self, callback, html):
		return

	def _okSetDslState(self, callback, html):
		return

	def _errorGetInfo(self, error):
		debug("[FritzCallFBF_05_27] _errorGetInfo: %s" % (error))
		text = _("FRITZ!Box - Error getting status: %s") % error.getErrorMessage()
		self._notify(text)
		return

	def reset(self):
		self._login(self._reset)

	def _reset(self, html):
		# POSTDATA=getpage=../html/reboot.html&errorpage=../html/de/menus/menu2.html&var:lang=de&var:pagename=home&var:errorpagename=home&var:menu=home&var:pagemaster=&time:settings/time=1242207340%2C-120&var:tabReset=0&logic:command/reboot=../gateway/commands/saveconfig.html
		if html:
			#===================================================================
			# found = re.match('.*<p class="errorMessage">FEHLER:&nbsp;([^<]*)</p>', html, re.S)
			# if found:
			#	self._errorReset('Login: ' + found.group(1))
			#	return
			#===================================================================
			start = html.find('<p class="errorMessage">FEHLER:&nbsp;')
			if start != -1:
				start = start + len('<p class="errorMessage">FEHLER:&nbsp;')
				self._errorReset('Login: ' + html[start, html.find('</p>', start)])
				return
		if self._callScreen:
			self._callScreen.close()
		url = "http://%s/cgi-bin/webcm" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'getpage':'../html/reboot.html',
			'var:lang':'de',
			'var:pagename':'reset',
			'var:menu':'system',
			'logic:command/reboot':'../gateway/commands/saveconfig.html',
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_27] _reset url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms)

	def _okReset(self, html): #@UnusedVariable # pylint: disable=W0613
		debug("[FritzCallFBF_05_27] _okReset")

	def _errorReset(self, error):
		debug("[FritzCallFBF_05_27] _errorReset: %s" % (error))
		text = _("FRITZ!Box - Error resetting: %s") % error.getErrorMessage()
		self._notify(text)

	def _readBlacklist(self):
		# http://fritz.box/cgi-bin/webcm?getpage=../html/de/menus/menu2.html&var:lang=de&var:menu=fon&var:pagename=sperre
		url = "http://%s/fon_num/sperre.lua" % config.plugins.FritzCall.hostname.value
		parms = urlencode({
			'sid':self._md5Sid
			})
		debug("[FritzCallFBF_05_27] _readBlacklist url: '" + url + "' parms: '" + parms + "'")
		getPage(url,
			method="POST",
			agent="Mozilla/5.0 (Windows; U; Windows NT 6.0; de; rv:1.9.0.5) Gecko/2008120122 Firefox/3.0.5",
			headers={
					'Content-Type': "application/x-www-form-urlencoded",
					'Content-Length': str(len(parms))},
			postdata=parms).addCallback(self._okBlacklist).addErrback(self._errorBlacklist)

	def _okBlacklist(self, html):
		debug("[FritzCallFBF_05_27] _okBlacklist")
		#=======================================================================
		# linkP = open("/tmp/FritzCallBlacklist.htm", "w")
		# linkP.write(html)
		# linkP.close()
		#=======================================================================
		entries = re.compile('<span title="(?:Ankommende|Ausgehende) Rufe">(Ankommende|Ausgehende) Rufe</span></nobr></td><td><nobr><span title="[\d]+">([\d]+)</span>', re.S).finditer(html)
		self.blacklist = ([], [])
		for entry in entries:
			if entry.group(1) == "Ankommende":
				self.blacklist[0].append(entry.group(2))
			else:
				self.blacklist[1].append(entry.group(2))
		debug("[FritzCallFBF_05_27] _okBlacklist: %s" % repr(self.blacklist))

	def _errorBlacklist(self, error):
		debug("[FritzCallFBF_05_27] _errorBlacklist: %s" % (error))
		text = _("FRITZ!Box - Error getting blacklist: %s") % error.getErrorMessage()
		self._notify(text)
