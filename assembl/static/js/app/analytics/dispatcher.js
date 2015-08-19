'use strict';

var _ = require('../shims/underscore.js'),
    $ = require('../shims/jquery.js'),
    Wrapper = require('./abstract.js'),
    Piwik = require('./piwik.js');


var AnalyticsDispatcher = function(){
  if(this.debug) {
    console.log("AnalyticsDispatcher constructor called");
  }
  Wrapper.call(this, arguments); 
  this._observers = [];
};

AnalyticsDispatcher.prototype = Object.create(Wrapper.prototype);
AnalyticsDispatcher.prototype.constructor = AnalyticsDispatcher;
_.extend(AnalyticsDispatcher.prototype, {

  registerObserver: function(observer) {
    this._observers.push(observer);
  },

  removeObserver: function(observer){
    delete this._observers.indexOf(observer); //empty slot
  },

  notify: function(methodName, args, check_cb){
    var that = this,
        check_cb = check_cb, 
        args = args;

    if(this.debug) {
      console.log("dispatching " + methodName + " on " + this._observers.length + " observer(s) with argument(s):", args);
    }

    _.each(this._observers, function(observer){
      if (check_cb !== undefined) {
        if (!check_cb(observer)) {
          return;
        }
      }
      if(observer.debug) {
        console.log('Invoking method ' + methodName + ' with argument(s)', args, 'on observer', observer);
      }
      if(observer[methodName]===undefined) {
        throw new Error('Method ' + methodName + ' does not exist');
      }
      observer[methodName].apply(observer, args);
    });
  },

  getObserver: function(index) {
    if (index >= 0 && index > (this._observers.length -1 )) {
      return this._observers[index]
    }
  },

  initialize: function(options){
    if(this.debug  && this._observers.length < 1) {
      console.warn("No observers registered for analytics");
    }
    this.notify('initialize', arguments);
  },

  changeCurrentPage: function(page, options) {
    if (!(page in this.pages)) {
      throw new Error("Unknown page definition");
    }
    this.notify('changeCurrentPage', arguments);
  },

  trackEvent: function(category, action, eventName, value, options) {
    if (!(eventName in this.events)) {
      throw new Exception("Unknown event type");
    }
    this.notify('trackEvent', arguments);
  },

  setCustomVariable: function(name, value, options){
    this.notify('setCustomVariable', arguments);
  },
 
  deleteCustomVariable: function(options){
    this.notify('deleteCustomVariable', arguments);
  },

  trackLink: function(urlPath, options){
    this.notify('trackEvent', arguments);
  },

  trackDomElement: function(element) {
    this.notify('trackDomElement', arguments);
  },

  trackGoal: function(goalId, options){
    this.notify('trackGoal', arguments, function(observer){
      // To add more checks for other Observer types, must update this check_cb
      if (observer.constructor.name === 'Piwik') {
        if (_.has(globalAnalytics.piwik.goals, goalId)){
          return true;
        }
        else { return false; }
      }
    });
  },

  createNewVisit: function(){
    this.notify('createNewVisit', []);
  },

  setUserId: function(id) {
    this.notify('setUserId', [id]);
  }
});

var _analytics;

module.exports = {
  /** A factory returning a completely setup singleton of a concrete analytics 
   * object.
   */
  getInstance: function(){
    if (!_analytics){
      _analytics = new AnalyticsDispatcher();
      if (_.has(window.globalAnalytics, 'piwik') && globalAnalytics.piwik.isActive) {
        if(_analytics.debug) {
          console.log("Registering piwik");
        }
        var p = new Piwik();
        _analytics.registerObserver(p);
      }
      else if (_.has(window.globalAnalytics, 'google') && globalAnalytics.google.isActive) {
        if(_analytics.debug) {
          console.log("Registering Google Analytics");
        }
        var g = null; //Where Google Analytics would go
        _analytics.registerObserver(g);
      }
      _analytics.initialize({engine: _paq}); //_paq might not be available at page load...
    }
    return _analytics;
  }
}
