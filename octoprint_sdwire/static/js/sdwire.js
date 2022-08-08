/*
 * View model for OctoPrint-Sdwire
 *
 * Author: Arkadiusz Miśkiewicz
 * License: AGPLv3
 */
$(function() {
    function SdwireViewModel(parameters) {
        var self = this;

        // assign the injected parameters, e.g.:
	self.filesViewModel = parameters[0];

	self.onDataUpdaterPluginMessage = function(plugin, data) {
		if (plugin != "sdwire") {
			return;
		}

		if (data.hasOwnProperty("progress")) {
			    self.filesViewModel._setProgressBar(data["progress"], 'Uploading to sdwire - ' + data["progress"] + '%...', false);
		}

                if (data.hasOwnProperty("error")) {
                    new PNotify({
                        title: 'Sdwire Error',
                        text: '<div class="row-fluid"><p>Looks like your settings are not correct or there was an error.</p><p><pre style="padding-top: 5px;">'+data["error"]+'</pre></p>',
                        hide: true
                    });
                    return;
                }
        }
    }

    /* view model class, parameters for constructor, container to bind to
     * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
     * and a full list of the available options.
     */
    OCTOPRINT_VIEWMODELS.push({
        construct: SdwireViewModel,
        dependencies: [ "filesViewModel" ]
    });
});
