preferredVendorSmart = [5, 9, 197, 198];

$(document).ready(function() {
  blkdevs.forEach(function(dev) {
    // Prefer the model_family, if it exists
    var model = dev.model;
    if (dev.model_family) {
      model = dev.model_family;
    }
    dev.nice_model = model;

    // Add in the best smart attributes
    _.each(preferredVendorSmart, function(id) {
      var smart = smarts[dev.serial];
      try {
        var name = smart.vendor_smart[id].name.replace(/_/g, " ");
        var value = smart.vendor_smart[id].raw;
      }
      catch (TypeError) {
        // Likely this disk does not support this smart attribute
        return;
      }
      dev["smart_" + id] = value;
    });
  });

  // Docs: http://js-grid.com/docs/
  $("#grid").jsGrid({
    width: "100%",
    height: "auto",

    inserting: false,
    editing: false,
    sorting: true,
    //paging: true,

    data: blkdevs,

    fields: [
      { name: "host", type: "text", width: "50", title: "Host" },
      { name: "kern_name", type: "text", width: "40", title: "Node" },
      { name: "size_bytes", type: "capacity", width: "50", title: "Size" },
      // Got icons here, loads more good ones: https://icons8.com/
      { name: "is_spinning_rust", type: "disktype", width: "50", title: "Disk Type" },
      { name: "nice_model", type: "text", width: "160", title: "Model" },
      { name: "serial", type: "text" },
      { name: "smart_9", type: "number", title: "Power On Hours" },
      { name: "smart_5", type: "text", title: "Reallocated Sectors" },
      { name: "smart_197", type: "text", title: "Current Pending Sectors" },
      { name: "smart_198", type: "text", title: "Offline Uncorrectable" }
      //{ name: "first_seen", type: "date", title: "First Seen" }
    ],

    rowClick: function(wrapt) {
      new DrivePopup(wrapt.item, this.fields);
    }
  }); 
});


function DrivePopup(metadata, shownFields) {
  var getAllSmart = function() {
    $.ajax("/getsmarts/" + metadata.serial, {
      dataType: "json",
      success: function(smartList) {
        console.log("got " + smartList.length + " smart logs");
      }
    });
  };

  // Build the heading and outer wrapper
  var content = $("<div>");
  content.append([
      $("<h1>", {
        html: metadata.host + ": " + metadata.kern_name
      }),
      $("<h2>", {
        html: metadata.nice_model
      }),
      $("<h3>", {
        html: "Select SMART:"
      })
  ]);

  // Add SMART values we think are important
  var smart = smarts[metadata.serial];
  var smartTable = $("<table>");
  _.each(preferredVendorSmart, function(id) {
    try {
      smartTable.append("<tr>").append([
          $("<td>", {
            html: smart.vendor_smart[id].name.replace(/_/g, " ")
          }),
          $("<td>", {
            html: smart.vendor_smart[id].raw
          })
      ]);
    }
    catch (TypeError) {
      // Likely this disk does not support this smart attribute
      return;
    }
  });
  smartTable.append("<tr>").append([
      $("<td>Error log count: </td>"),
      $("<td>" + smart.errors.error_count + "</td>")
  ]);
  content.append(smartTable);

  // Add evey metadata field that is not shown in the main grid
  var propsToRemove = _.pluck(shownFields, "name");
  var fields = _.omit(metadata, propsToRemove);
  var metaTable = $("<table>");
  _.each(_.sortBy(_.keys(fields)), function(key) {
    metaTable.append("<tr>").append([
      $("<td>" + key + "</td>"),
      $("<td>" + fields[key] + "</td>")
    ]);
  });
  content.append([
      $("<h3>", {
        html: "Other details:"
      }),
      metaTable
  ]);

  var popup = new tingle.modal();
  popup.setContent(content[0]);
  popup.open();
};
