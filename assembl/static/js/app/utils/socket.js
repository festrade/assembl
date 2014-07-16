define(["modules/assembl", 'modules/context', 'underscore', 'sockjs'], function(Assembl, Ctx, _, SockJS){
    'use strict';

    /**
     * @class Socket
     *
     * @param {string} url
     */
    var Socket = function(){
        this.init();
    };

    /**
     * @const
     */
    Socket.STATE_CLOSED = 0;
    Socket.STATE_CONNECTING = 1;
    Socket.STATE_OPEN = 2;
    Socket.CONNECTION_TIMEOUT_TIME = 5000;

    /**
     * @init
     */
    Socket.prototype.init = function(){
        this.socket = new SockJS(Ctx.getSocketUrl());
        this.socket.onopen = this.onOpen.bind(this);
        this.socket.onmessage = this.onMessage.bind(this);
        this.socket.onclose = this.onClose.bind(this);
        this.state = Socket.STATE_CLOSED;
    };

    /**
     * Triggered when the connection opens
     * @event
     */
    Socket.prototype.onOpen = function(){
        this.socket.send("token:" + Ctx.getCsrfToken());
        this.socket.send("discussion:" + Ctx.getDiscussionId());
        this.state = Socket.STATE_CONNECTING;
    };

    /**
     * Triggered when the client receives a message form server
     * @event
     */
    Socket.prototype.onMessage = function(ev){
        if (this.state == Socket.STATE_CONNECTING) {
            Assembl.commands.execute('socket:open');
            this.state = Socket.STATE_OPEN;
        }
        var data = JSON.parse(ev.data),
            i = 0,
            len = data.length;

        for(; i<len; i += 1){
            this.processData(data[i]);
        }

        Assembl.commands.execute('socket:message');
    };

    /**
     * Triggered when the connection closes ( or lost the connection )
     * @event
     */
    Socket.prototype.onClose = function(){
        Assembl.commands.execute('socket:close');
        
        var that = this;
        window.setTimeout(function(){
            that.init();
        }, Socket.CONNECTION_TIMEOUT_TIME);
    };

    /**
     * Processes one item from a data array from the server
     * @param  {Object]} item
     */
    Socket.prototype.processData = function(item){
        var collection = Ctx.getCollectionByType(item);

        if (Ctx.debugSocket) {
            console.log( item['@id'] || item['@type'], item );
        }

        if( collection === null ){
            if(item['@type'] == "Connection") {
                //Ignore Connections
                return;
            } else {
                if (Ctx.debugSocket) {
                    console.log("Socket.prototype.processData(): TODO: Handle singletons like discussion etc. for item:", item);
                }
                return;
            }

        }
        // Each collection must know what to do
        collection.updateFromSocket(item);
    };

    return Socket;
});
